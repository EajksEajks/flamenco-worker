"""Queues task updates to Flamenco Manager.

Task updates are pickled and stored in a SQLite database. Pickling allows
for efficient conversion of Python objects into a binary data blob.
"""

import asyncio
import pickle
import sqlite3
import typing

import attr

from . import attrs_extra, upstream

BACKOFF_TIME = 5  # seconds
SHUTDOWN_RECHECK_TIME = 0.5  # seconds


@attr.s
class TaskUpdateQueue:
    manager = attr.ib(validator=attr.validators.instance_of(upstream.FlamencoManager))
    shutdown_future = attr.ib(
        validator=attr.validators.optional(attr.validators.instance_of(asyncio.Future)))
    db_fname = attr.ib(validator=attr.validators.instance_of(str))

    backoff_time = attr.ib(default=BACKOFF_TIME)
    shutdown_recheck_time = attr.ib(default=SHUTDOWN_RECHECK_TIME)

    _stuff_queued = attr.ib(default=attr.Factory(asyncio.Event), init=False)
    _db = attr.ib(default=None, init=False)
    _queue_lock = attr.ib(default=attr.Factory(asyncio.Lock), init=False)
    _log = attrs_extra.log('%s.TaskUpdateQueue' % __name__)

    def _connect_db(self):
        self._log.info('Connecting to database %s', self.db_fname)
        self._db = sqlite3.connect(self.db_fname, isolation_level=None)

        # We don't need to create a primary key; we use the implicit 'rowid' column.
        self._db.execute('CREATE TABLE IF NOT EXISTS fworker_queue(url TEXT, payload BLOB)')

        # Start with a more-or-less compact database.
        self._db.execute('VACUUM')

        # Now that that is out of the way, we can use the default SQLite behaviour again.
        self._db.isolation_level = ''

    def _disconnect_db(self):
        self._log.info('Disconnecting from database %s', self.db_fname)
        self._db.close()
        self._db = None

    def queue(self, url, payload):
        """Push some payload onto the queue."""

        if self._db is None:
            self._connect_db()

        # Store the pickled payload in the SQLite database.
        pickled = pickle.dumps(payload)

        self._db.execute('INSERT INTO fworker_queue (url, payload) values (?, ?)',
                         (url, pickled))
        self._db.commit()

        # Notify the work loop that stuff has been queued.
        self._stuff_queued.set()

    async def work(self, *, loop=None):
        """Loop that pushes queued payloads to the Flamenco Manager.

        Keeps running until shutdown_future.done() returns True.
        """

        # Always start by inspecting the persisted queue, so act as if something
        # was just queued.
        self._stuff_queued.set()

        while not self.shutdown_future.done():
            try:
                await asyncio.wait_for(self._stuff_queued.wait(),
                                       self.shutdown_recheck_time,
                                       loop=loop)
            except asyncio.TimeoutError:
                # This is normal, it just means that there wasn't anything queued within
                # SHUTDOWN_RECHECK_TIME seconds.
                continue
            except asyncio.CancelledError:
                # We're being shut down.
                break

            self._log.debug('Inspecting queued task updates.')
            await self.flush_and_catch(loop=loop)
        self._log.warning('Stopping work loop')

    def _queue(self) -> typing.Iterable[typing.Tuple[int, str, object]]:
        """Yields (rowid, url, unpickled payload) tuples from the database."""

        if self._db is None:
            self._connect_db()

        result = self._db.execute('''
            SELECT rowid, url, payload
            FROM fworker_queue
            ORDER BY rowid ASC
        ''')
        for row in result:
            rowid = row[0]
            url = row[1]
            payload = pickle.loads(row[2])
            yield rowid, url, payload

    def queue_size(self) -> int:
        """Return the number of items queued."""
        if self._db is None:
            self._connect_db()

        result = self._db.execute('SELECT count(*) FROM fworker_queue')
        count = next(result)[0]
        return count

    def _unqueue(self, rowid: int):
        """Removes a queued payload from the database."""

        # TODO Sybren: every once in a while, run 'vacuum' on the database.
        self._db.execute('DELETE FROM fworker_queue WHERE rowid=?', (rowid,))
        self._db.commit()

    async def flush(self, *, loop: asyncio.AbstractEventLoop) -> bool:
        """Tries to flush the queue to the Manager.

        Returns True iff the queue was empty, even before flushing.
        """

        with (await self._queue_lock):
            queue_is_empty = True
            queue_size_before = self.queue_size()
            handled = 0
            for rowid, url, payload in self._queue():
                queue_is_empty = False

                queue_size = self.queue_size()
                self._log.info('Pushing task update to Manager, queue size is %d', queue_size)
                resp = await self.manager.post(url, json=payload, loop=loop)
                if resp.status_code == 409:
                    # The task was assigned to another worker, so we're not allowed to
                    # push updates for it. We have to un-queue this update, as it will
                    # never be accepted.
                    self._log.warning('discarding update, Manager says %s', resp.text)
                    # TODO(sybren): delete all queued updates to the same URL?
                else:
                    resp.raise_for_status()
                    self._log.debug('Master accepted pushed update.')
                self._unqueue(rowid)

                handled += 1
                if handled > 1000:
                    self._log.info('Taking a break from queue flushing after %d items', handled)
                    break

            if queue_is_empty:
                # Only clear the flag once the queue has really been cleared.
                self._stuff_queued.clear()

            queue_size_after = self.queue_size()
            if queue_size_after > 0:
                if queue_size_after >= queue_size_before:
                    self._log.warning(
                        'Queue size increased from %d to %d, after having flushed %d items',
                        queue_size_before, queue_size_after, handled)
                else:
                    self._log.info(
                        'Queue size decreased from %d to %d, after having flushed %d items',
                        queue_size_before, queue_size_after, handled)

            return queue_is_empty

    async def flush_and_report(self, *, loop: asyncio.AbstractEventLoop):
        """Flushes the queue, and just reports errors, doesn't wait nor retry."""

        import requests

        self._log.info('flush_and_report: trying one last push to get updates to Manager')

        try:
            await self.flush(loop=loop)
        except requests.ConnectionError:
            self._log.warning('flush_and_report: Unable to connect to Manager, '
                              'some items are still queued.')
        except requests.HTTPError as ex:
            self._log.warning('flush_and_report: Manager did not accept our updates (%s),'
                              ' some items are still queued.', ex)
        except Exception:
            self._log.exception('flush_and_report: Unexpected exception, '
                                'Some items are still queued.')

    async def flush_and_catch(self, *, loop: asyncio.AbstractEventLoop):
        """Flushes the queue, reports errors and waits before returning for another try."""

        import requests

        try:
            await self.flush(loop=loop)
        except requests.ConnectionError:
            self._log.warning('Unable to connect to Manager, will retry later.')
            await asyncio.sleep(self.backoff_time)
        except requests.HTTPError as ex:
            self._log.warning('Manager did not accept our updates (%s), will retry later.',
                              ex)
            await asyncio.sleep(self.backoff_time)
        except Exception:
            self._log.exception('Unexpected exception in work loop. '
                                'Backing off and retring later.')
            await asyncio.sleep(self.backoff_time)
