from pathlib import Path
import subprocess
from unittest import mock

from unittest.mock import patch

from tests.test_runner import AbstractCommandTest

expected_script = """
import bpy
bpy.context.scene.frame_start = 1
bpy.context.scene.frame_end = 47
bpy.ops.sound.mixdown(filepath='/tmp/output.flac', codec='FLAC', container='FLAC', accuracy=128)
bpy.ops.wm.quit_blender()
""".strip('\n')


class RenderAudioTest(AbstractCommandTest):
    def setUp(self):
        super().setUp()

        from flamenco_worker.commands import BlenderRenderAudioCommand

        self.cmd = BlenderRenderAudioCommand(
            worker=self.fworker,
            task_id='12345',
            command_idx=0,
        )

    def test_cli_args(self):
        from tests.mock_responses import CoroMock

        filepath = Path(__file__)
        settings = {
            # Point blender_cmd to this file so that we're sure it exists.
            'blender_cmd': '%s --with --cli="args for CLI"' % __file__,
            'frame_start': 1,
            'frame_end': 47,
            'filepath': str(filepath),
            'render_output': '/tmp/output.flac',
        }

        cse = CoroMock(...)
        cse.coro.return_value.wait = CoroMock(return_value=0)
        cse.coro.return_value.pid = 47
        with patch('asyncio.create_subprocess_exec', new=cse) as mock_cse:
            self.loop.run_until_complete(self.cmd.run(settings))

            mock_cse.assert_called_once_with(
                __file__,
                '--with',
                '--cli=args for CLI',
                '--enable-autoexec',
                '-noaudio',
                '--background',
                str(filepath),
                '--python-exit-code', '47',
                '--python-expr', expected_script,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
