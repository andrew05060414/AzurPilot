import unittest
from unittest.mock import Mock, patch

from module.webui.base import Base
from module.webui.process_manager import ProcessManager


class TestWebUISessionLifecycle(unittest.TestCase):
    def test_session_disconnect_does_not_stop_running_workers(self):
        worker = Mock()
        page = Base.__new__(Base)
        page.alive = True
        page.task_handler = Mock()

        with patch.object(ProcessManager, "_processes", {"alas": worker}):
            page.stop()

        self.assertFalse(page.alive)
        page.task_handler.stop.assert_called_once_with()
        worker.stop.assert_not_called()
