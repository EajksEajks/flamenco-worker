"""
Patches a safer version of resume_reading into the asyncio.unix_events._UnixReadPipeTransport class.

This prevents an error at the end of a subprocess execution:

    File "/usr/lib/python3.x/asyncio/unix_events.py", line 364, in resume_reading
       self._loop.add_reader(self._fileno, self._read_ready)
    AttributeError: 'NoneType' object has no attribute 'add_reader'

"""


def patch_asyncio():
    import logging
    import sys

    log = logging.getLogger(__name__)
    if sys.platform == 'win32':
        log.debug('Patching ue._UnixReadPipeTransport.resume_reading not needed on Windows')
        return

    log.debug('Patching ue._UnixReadPipeTransport.resume_reading')

    import asyncio.unix_events as ue
    orig_resume_reading = ue._UnixReadPipeTransport.resume_reading

    def resume_reading(self, *args, **kwargs):
        if not self._loop:
            return

        return orig_resume_reading(self, *args, **kwargs)

    ue._UnixReadPipeTransport.resume_reading = resume_reading
