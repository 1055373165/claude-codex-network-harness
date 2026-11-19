import json
import queue
import subprocess
import sys
import threading
import unittest


SERVER = """
import json
import sys

def emit(message):
    print(json.dumps(message), flush=True)

requests = []
for line in sys.stdin:
    req = json.loads(line)
    method = req["method"]
    params = req.get("params", {})
    requests.append(method)
    if method == "turn/start" and params["threadId"] == "broken":
        emit({"id": req["id"], "result": {}})
        emit({"method": "error", "params": {
            "threadId": "broken", "turnId": "turn", "willRetry": True,
            "error": {"codexErrorInfo": {"responseStreamDisconnected": {}}},
        }})
        continue
    if method == "turn/interrupt":
        emit({"id": req["id"], "result": {}})
        emit({"method": "turn/completed", "params": {
            "threadId": "broken", "turn": {"id": "turn", "status": "interrupted"},
        }})
        continue
    if method == "thread/unsubscribe":
        emit({"id": req["id"], "result": {"status": "unsubscribed"}})
        continue
    result = {"requests": requests} if method == "fixture/requests" else params
    emit({"id": req["id"], "result": result})
"""


class CodexProtocolTests(unittest.TestCase):
    def test_interrupts_before_replay_and_detaches_only_the_failed_thread(self):
        launcher = (
            "import sys; from network_harness.codex import run; "
            "sys.exit(run(sys.executable, ['-u', '-c', sys.argv[1], 'app-server']))"
        )
        with subprocess.Popen([sys.executable, "-u", "-c", launcher, SERVER],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as child:
            responses = queue.Queue()

            def read():
                for line in child.stdout:
                    responses.put(json.loads(line))

            worker = threading.Thread(target=read, daemon=True)
            worker.start()

            def send(i, method, params):
                child.stdin.write(json.dumps({"id": i, "method": method, "params": params}) + "\n")
                child.stdin.flush()

            def next_message():
                return responses.get(timeout=5)

            try:
                send(1, "thread/resume", {"threadId": "broken", "modelProvider": "openai"})
                self.assertEqual(next_message()["result"]["modelProvider"], "openai")
                send(6, "thread/resume", {"threadId": "older"})
                self.assertNotIn("modelProvider", next_message()["result"])
                send(7, "thread/start", {"modelProvider": "openai"})
                self.assertEqual(next_message()["result"]["modelProvider"], "openai_fail_fast")
                send(2, "turn/start", {"threadId": "broken"})
                self.assertEqual(next_message()["id"], 2)
                error = next_message()
                self.assertEqual(error["method"], "error")
                self.assertFalse(error["params"]["willRetry"])
                self.assertEqual(next_message()["method"], "turn/completed")

                send(3, "thread/resume", {"threadId": "broken"})
                self.assertIn("Session disconnected", next_message()["error"]["message"])
                send(4, "turn/start", {"threadId": "healthy"})
                self.assertEqual(next_message()["result"]["threadId"], "healthy")
                send(5, "fixture/requests", {})
                self.assertEqual(next_message()["result"]["requests"], [
                    "thread/resume", "thread/resume", "thread/start", "turn/start",
                    "turn/interrupt", "thread/unsubscribe",
                    "turn/start", "fixture/requests",
                ])
            finally:
                child.stdin.close()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                worker.join(timeout=1)
