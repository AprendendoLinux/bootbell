# -*- coding: utf-8 -*-
"""Testes do bootbell (sem rede: usam um falso servidor da Bot API).

Rodar:  python3 -m unittest discover -s tests -v

Por padrão testa src/bootbell.py. Para testar o programa instalado pelo
pacote: BOOTBELL_MODULE=/usr/bin/bootbell python3 -m unittest ...
Compatível com Python 3.6.
"""

import http.server
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE_PATH = os.environ.get("BOOTBELL_MODULE",
                             os.path.join(HERE, "..", "src", "bootbell.py"))


def load_module():
    loader = importlib.machinery.SourceFileLoader("bootbell", MODULE_PATH)
    spec = importlib.util.spec_from_loader("bootbell", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


bootbell = load_module()
bootbell.RETRY_INITIAL_DELAY = 0.05
bootbell.RETRY_MAX_DELAY = 0.1
bootbell.IP_WAIT_INTERVAL = 0.05

TOKEN = "123456:ABCdef"

IP_OUTPUT = """\
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
1: lo    inet6 ::1/128 scope host noprefixroute \\       valid_lft forever preferred_lft forever
2: enp3s0    inet 10.0.0.2/28 brd 10.0.0.15 scope global dynamic noprefixroute enp3s0\\       valid_lft 251sec preferred_lft 251sec
2: enp3s0    inet6 2001:db8::2/64 scope global noprefixroute \\       valid_lft forever preferred_lft forever
2: enp3s0    inet6 fe80::1/64 scope link noprefixroute \\       valid_lft forever preferred_lft forever
3: virbr0    inet 192.168.122.1/24 brd 192.168.122.255 scope global virbr0\\       valid_lft forever preferred_lft forever
4: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\\       valid_lft forever preferred_lft forever
5: wlan0@if9    inet 192.168.0.10/24 scope global wlan0\\       valid_lft forever preferred_lft forever
"""


class FakeTelegram(object):
    """Servidor HTTP local que imita POST /bot<token>/sendMessage."""

    def __init__(self):
        self.received = []
        self.responses = []  # lista de (status, corpo); vazia = sucesso
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
                fake.received.append({"path": self.path,
                                      "form": {k: v[0] for k, v in form.items()}})
                if fake.responses:
                    status, body = fake.responses.pop(0)
                else:
                    status, body = 200, {"ok": True, "result": {}}
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:{}".format(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def texts(self):
        return [item["form"]["text"] for item in self.received]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class BaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_dir = os.path.join(self.tmp, "state")
        self.fake = FakeTelegram()
        self.addCleanup(self.fake.close)
        self.addCleanup(shutil.rmtree, self.tmp)
        self.system = {"state": "starting", "action": None,
                       "boot_id": "boot-2", "uptime": 42.0}
        for name, value in (
                ("system_state", lambda: self.system["state"]),
                ("pending_shutdown_action", lambda: self.system["action"]),
                ("boot_id", lambda: self.system["boot_id"]),
                ("uptime_seconds", lambda: self.system["uptime"]),
                ("get_ip_addresses", lambda patterns: [("eth0", "10.0.0.2")])):
            patcher = mock.patch.object(bootbell, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config_path = self.write_config()

    def write_config(self, token=TOKEN, chat_id="999", api_url=None,
                     telegram="", general="", messages=""):
        path = os.path.join(self.tmp, "bootbell.conf")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                "[telegram]\ntoken = {}\nchat_id = {}\napi_url = {}\n{}"
                "[general]\nhostname = maquina\nboot_retry_for = 1\n"
                "shutdown_timeout = 1\n{}\n"
                "[messages]\n{}".format(
                    token, chat_id, api_url or self.fake.url, telegram,
                    general or "request_timeout = 2\n", messages))
        return path

    def run_cli(self, *args):
        stdout, stderr = io.BytesIO(), io.BytesIO()
        out = io.TextIOWrapper(stdout, encoding="utf-8")
        err = io.TextIOWrapper(stderr, encoding="utf-8")
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            code = bootbell.main(["-c", self.config_path, "--state-dir", self.state_dir]
                                 + list(args))
            out.flush()
            err.flush()
        return code, stdout.getvalue().decode("utf-8"), stderr.getvalue().decode("utf-8")

    def state_file(self, name):
        path = os.path.join(self.state_dir, name)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)


class FormattingTests(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(bootbell.format_duration(None), "tempo desconhecido")
        self.assertEqual(bootbell.format_duration(30), "menos de um minuto")
        self.assertEqual(bootbell.format_duration(60), "1 minuto")
        self.assertEqual(bootbell.format_duration(3600), "1 hora")
        self.assertEqual(bootbell.format_duration(2 * 3600 + 5 * 60), "2 horas e 5 minutos")
        self.assertEqual(bootbell.format_duration(86400 + 3600 + 60),
                         "1 dia, 1 hora e 1 minuto")
        self.assertEqual(bootbell.format_duration(3 * 86400), "3 dias")

    def test_parse_ip_output_filters_scope_and_ignored(self):
        result = bootbell.parse_ip_output(IP_OUTPUT, ["docker*", "virbr*"])
        self.assertEqual(result, [("enp3s0", "10.0.0.2"),
                                  ("enp3s0", "2001:db8::2"),
                                  ("wlan0", "192.168.0.10")])

    def test_parse_ip_output_without_ignore(self):
        interfaces = [i for i, _ in bootbell.parse_ip_output(IP_OUTPUT, [])]
        self.assertIn("docker0", interfaces)
        self.assertNotIn("lo", interfaces)

    def test_format_ips(self):
        self.assertEqual(bootbell.format_ips([]), "nenhum endereço encontrado")
        self.assertEqual(
            bootbell.format_ips([("a", "1"), ("b", "2"), ("a", "3")]),
            "• a: 1, 3\n• b: 2")

    def test_render(self):
        self.assertEqual(bootbell.render("oi {host}\\nfim", {"host": "pc"}), "oi pc\nfim")
        self.assertEqual(bootbell.render("{desconhecido} {host}", {"host": "pc"}),
                         "{desconhecido} pc")
        self.assertEqual(bootbell.render("quebrado {", {}), "quebrado {")
        self.assertEqual(bootbell.render("{0}", {}), "{0}")


class ConfigTests(BaseTest):
    def test_bot_prefix_is_removed(self):
        self.write_config(token="bot" + TOKEN)
        self.assertEqual(bootbell.Config(self.config_path).token, TOKEN)

    def test_missing_token(self):
        self.write_config(token="")
        code, _, err = self.run_cli("boot")
        self.assertEqual(code, 1)
        self.assertIn("token não foi configurado", err)

    def test_missing_file(self):
        self.config_path = os.path.join(self.tmp, "nao-existe.conf")
        code, _, err = self.run_cli("test")
        self.assertEqual(code, 1)
        self.assertIn("não encontrado", err)

    def test_invalid_number(self):
        self.write_config(general="request_timeout = abc\n")
        with self.assertRaises(bootbell.ConfigError):
            bootbell.Config(self.config_path)

    def test_custom_message_and_percent_sign(self):
        self.write_config(messages="test = 100% {host}\n")
        code, out, _ = self.run_cli("--dry-run", "test")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "100% maquina")

    def test_shutdown_with_broken_config_never_fails(self):
        self.write_config(token="")
        code, _, _ = self.run_cli("shutdown")
        self.assertEqual(code, 0)

    def test_check_does_not_send(self):
        code, out, _ = self.run_cli("check")
        self.assertEqual(code, 0)
        self.assertIn("Configuração: OK", out)
        self.assertIn("bot 123456", out)
        self.assertNotIn("ABCdef", out)
        self.assertEqual(self.fake.received, [])


class SendTests(BaseTest):
    def test_test_command_sends_to_api(self):
        code, out, _ = self.run_cli("test")
        self.assertEqual(code, 0)
        self.assertIn("enviada", out)
        request = self.fake.received[0]
        self.assertEqual(request["path"], "/bot{}/sendMessage".format(TOKEN))
        self.assertEqual(request["form"]["chat_id"], "999")
        self.assertIn("maquina", request["form"]["text"])
        self.assertNotIn("disable_notification", request["form"])

    def test_silent(self):
        self.write_config(telegram="silent = yes\n")
        self.run_cli("test")
        self.assertEqual(self.fake.received[0]["form"]["disable_notification"], "true")

    def test_permanent_error_is_not_retried(self):
        self.fake.responses = [(401, {"ok": False, "description": "Unauthorized"})]
        code, _, err = self.run_cli("test")
        self.assertEqual(code, 1)
        self.assertIn("Unauthorized", err)
        self.assertNotIn(TOKEN, err)
        self.assertEqual(len(self.fake.received), 1)

    def test_temporary_error_is_retried(self):
        self.fake.responses = [(502, {"ok": False, "description": "Bad Gateway"})]
        code, _, _ = self.run_cli("test")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.fake.received), 2)

    def test_network_error_message_hides_token(self):
        self.write_config(api_url="http://127.0.0.1:9")
        code, _, err = self.run_cli("test")
        self.assertEqual(code, 1)
        self.assertIn("falha de rede", err)
        self.assertNotIn(TOKEN, err)


class LifecycleTests(BaseTest):
    def test_boot_sends_and_saves_session(self):
        code, _, _ = self.run_cli("boot")
        self.assertEqual(code, 0)
        texts = self.fake.texts()
        self.assertEqual(len(texts), 1)
        self.assertIn("LIGADO", texts[0])
        self.assertIn("• eth0: 10.0.0.2", texts[0])
        self.assertEqual(self.state_file("session.json")["boot_id"], "boot-2")

    def test_manual_start_sends_started(self):
        self.system["state"] = "running"
        self.run_cli("boot")
        self.assertIn("ATIVADO", self.fake.texts()[0])

    def test_clean_cycle_has_no_unclean_warning(self):
        self.run_cli("boot")
        self.system["state"] = "stopping"
        self.system["action"] = "reboot"
        self.system["uptime"] = 7200
        self.run_cli("shutdown")
        self.assertIsNone(self.state_file("session.json"))
        self.system.update(state="starting", boot_id="boot-3", uptime=30)
        self.run_cli("boot")
        texts = self.fake.texts()
        self.assertEqual(len(texts), 3)
        self.assertIn("vai REINICIAR", texts[1])
        self.assertIn("2 horas", texts[1])
        self.assertIn("LIGADO", texts[2])
        self.assertFalse(any("NÃO foi desligado" in text for text in texts))

    def test_poweroff_and_unknown_action(self):
        self.system.update(state="stopping", action="poweroff")
        self.run_cli("shutdown")
        self.system["action"] = None
        self.run_cli("shutdown")
        texts = self.fake.texts()
        self.assertIn("vai DESLIGAR em", texts[0])
        self.assertIn("vai DESLIGAR/REINICIAR em", texts[1])

    def test_manual_stop_sends_stopped(self):
        self.system["state"] = "running"
        self.run_cli("shutdown")
        self.assertIn("DESATIVADO", self.fake.texts()[0])

    def test_unclean_shutdown_detected(self):
        self.run_cli("boot")
        # Sem "shutdown": a máquina caiu. Novo boot com outro boot_id.
        self.system["boot_id"] = "boot-3"
        self.run_cli("boot")
        texts = self.fake.texts()
        self.assertEqual(len(texts), 3)
        self.assertIn("NÃO foi desligado", texts[1])
        self.assertIn("LIGADO", texts[2])

    def test_restart_in_same_boot_is_not_unclean(self):
        self.run_cli("boot")
        self.system["state"] = "running"
        self.run_cli("boot")
        self.assertFalse(any("NÃO foi desligado" in t for t in self.fake.texts()))

    def test_failed_shutdown_message_is_queued_and_sent_on_next_boot(self):
        self.write_config(api_url="http://127.0.0.1:9")
        self.system.update(state="stopping", action="poweroff")
        self.assertEqual(self.run_cli("shutdown")[0], 0)
        queue = self.state_file("queue.json")
        self.assertEqual(len(queue), 1)
        self.assertIn("vai DESLIGAR", queue[0]["text"])

        self.write_config()
        self.system.update(state="starting", boot_id="boot-3")
        self.run_cli("boot")
        texts = self.fake.texts()
        self.assertEqual(len(texts), 2)
        self.assertIn("vai DESLIGAR", texts[0])
        self.assertIn("entregue com atraso", texts[0])
        self.assertIn("LIGADO", texts[1])
        self.assertIsNone(self.state_file("queue.json"))

    def test_queue_is_limited(self):
        state = bootbell.State(self.state_dir)
        state.save_queue([{"text": str(i)} for i in range(50)])
        self.assertEqual(len(state.load_queue()), bootbell.MAX_QUEUE)
        self.assertEqual(state.load_queue()[-1]["text"], "49")

    def test_corrupt_state_is_ignored(self):
        os.makedirs(self.state_dir)
        with open(os.path.join(self.state_dir, "session.json"), "w") as handle:
            handle.write("{lixo")
        code, _, err = self.run_cli("boot")
        self.assertEqual(code, 0)
        self.assertIn("ilegível", err)
        self.assertEqual(len(self.fake.texts()), 1)

    def test_dry_run_changes_nothing(self):
        code, out, _ = self.run_cli("--dry-run", "boot")
        self.assertEqual(code, 0)
        self.assertIn("LIGADO", out)
        self.assertEqual(self.fake.received, [])
        self.assertFalse(os.path.exists(self.state_dir))

    def _late_ipv4(self, calls_without_ipv4):
        calls = []

        def fake_addresses(patterns):
            calls.append(1)
            if len(calls) <= calls_without_ipv4:
                return [("enp3s0", "2001:db8:20::2")]
            return [("enp3s0", "10.71.69.2"), ("enp3s0", "2001:db8:20::2")]
        return calls, fake_addresses

    def test_boot_waits_for_late_dhcp_ipv4(self):
        # IPv6 fixo já está no ar; o IPv4 do DHCP chega depois.
        calls, fake = self._late_ipv4(3)
        with mock.patch.object(bootbell, "get_ip_addresses", fake):
            self.run_cli("boot")
        text = self.fake.texts()[0]
        self.assertIn("• enp3s0: 10.71.69.2, 2001:db8:20::2", text)
        self.assertEqual(len(calls), 4)

    def test_boot_ipv6_only_gives_up_after_ip_wait(self):
        self.write_config(general="request_timeout = 2\nip_wait = 0.3\n")
        calls, fake = self._late_ipv4(10 ** 6)
        started = time.monotonic()
        with mock.patch.object(bootbell, "get_ip_addresses", fake):
            _, _, err = self.run_cli("boot")
        self.assertLess(time.monotonic() - started, 5)
        self.assertIn("nenhum endereço IPv4", err)
        self.assertIn("• enp3s0: 2001:db8:20::2", self.fake.texts()[0])

    def test_ip_wait_zero_and_manual_start_do_not_wait(self):
        self.write_config(general="request_timeout = 2\nip_wait = 0\n")
        calls, fake = self._late_ipv4(10 ** 6)
        with mock.patch.object(bootbell, "get_ip_addresses", fake):
            self.run_cli("boot")
            self.write_config()
            self.system["state"] = "running"
            self.run_cli("boot")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.fake.texts()), 2)

    def test_boot_does_not_block_forever(self):
        self.write_config(api_url="http://127.0.0.1:9")
        started = time.monotonic()
        self.assertEqual(self.run_cli("boot")[0], 0)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(len(self.state_file("queue.json")), 1)


if __name__ == "__main__":
    unittest.main()
