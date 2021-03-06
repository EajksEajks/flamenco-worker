from unittest.mock import Mock

from tests.abstract_worker_test import AbstractWorkerTest


class MayIRunTest(AbstractWorkerTest):
    def setUp(self):
        from datetime import timedelta

        from flamenco_worker.may_i_run import MayIRun
        from flamenco_worker.upstream import FlamencoManager
        from flamenco_worker.worker import FlamencoWorker
        from flamenco_worker.cli import construct_asyncio_loop

        self.loop = construct_asyncio_loop()
        self.manager = Mock(spec=FlamencoManager)
        self.worker = Mock(spec=FlamencoWorker)
        self.shutdown_future = self.loop.create_future()

        self.mir = MayIRun(manager=self.manager,
                           worker=self.worker,
                           poll_interval=timedelta(seconds=0.2),
                           loop=self.loop)

    def tearDown(self):
        if self.loop:
            self.loop.close()

    def _mock_get(self, *json_responses):
        from collections import deque

        values = deque(json_responses)

        async def mocked_get(*args, **kwargs):
            mocked_response = Mock()
            mocked_response.json.return_value = values.popleft()
            return mocked_response

        self.manager.get = mocked_get

    def test_may_i_run_false(self):
        self._mock_get({
            'may_keep_running': False,
            'reason': 'je moeder',
        })

        result = self.loop.run_until_complete(self.mir.may_i_run('1234'))
        self.assertFalse(result)

    def test_may_i_run_true(self):
        self._mock_get({
            'may_keep_running': True,
        })

        result = self.loop.run_until_complete(self.mir.may_i_run('1234'))
        self.assertTrue(result)

    def test_work(self):
        import asyncio

        self._mock_get(
            {'may_keep_running': True},
            {'may_keep_running': False, 'reason': 'unittesting'},
            # After this response, no more calls should be made.
        )

        # Let the 'stop_current_task()' call trigger an event.
        stop_event = asyncio.Event()
        async def set_stop_event():
            stop_event.set()
        self.worker.stop_current_task.side_effect = [set_stop_event()]

        # The work task should call may_i_run, which should, after two calls, stop the current task.
        work_task = asyncio.ensure_future(self.mir.work())

        self.loop.run_until_complete(asyncio.wait_for(
            stop_event.wait(),
            0.6
        ))

        # Cleanly shut down the work task.
        work_task.cancel()
        self.loop.run_until_complete(work_task)

    def test_go_asleep(self):
        self._mock_get(
            {'may_keep_running': False, 'reason': 'switching status', 'status_requested': 'Сергей'},
            # After this response, no more calls should be made.
        )

        result = self.loop.run_until_complete(self.mir.may_i_run('1234'))
        self.assertFalse(result)
        self.worker.change_status.assert_called_with('Сергей')
