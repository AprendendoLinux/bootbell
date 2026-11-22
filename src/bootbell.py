#!/usr/bin/python3
# -*- coding: utf-8 -*-
#
# bootbell - avisa no Telegram quando o computador liga ou desliga.
#
# Copyright (c) 2026 AprendendoLinux
# Distribuído sob a licença MIT (veja o arquivo LICENSE).
#
# IMPORTANTE: este programa precisa rodar em Python 3.6 ou mais novo
# (Ubuntu 18.04 / Debian 10 / Linux Mint 19 em diante). Use somente a
# biblioteca padrão e evite recursos introduzidos depois do Python 3.6
# (dataclasses, walrus, subprocess.run(capture_output=...), fromisoformat...).
"""bootbell - avisa no Telegram quando o computador liga ou desliga."""

import argparse
import collections
import configparser
import contextlib
import fnmatch
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

__version__ = "1.0.1"

DEFAULT_CONFIG = "/etc/bootbell/bootbell.conf"
DEFAULT_STATE_DIR = "/var/lib/bootbell"

SESSION_FILE = "session.json"
QUEUE_FILE = "queue.json"
MAX_QUEUE = 20

# Espera inicial entre tentativas de envio (dobra a cada falha, até 30s).
RETRY_INITIAL_DELAY = 2.0
RETRY_MAX_DELAY = 30.0

# Se não for possível consultar o systemd, uma máquina ligada há menos
# que isso é considerada "em processo de boot".
BOOT_UPTIME_FALLBACK = 600

# Intervalo entre verificações enquanto espera o IPv4 aparecer no boot.
IP_WAIT_INTERVAL = 1.0

BOOTING_STATES = ("initializing", "starting")
RUNNING_STATES = ("running", "degraded", "maintenance")
REBOOT_TARGETS = ("reboot.target", "kexec.target", "soft-reboot.target")
POWEROFF_TARGETS = ("poweroff.target", "halt.target")
ACTION_NAMES = {"reboot": "REINICIAR", "poweroff": "DESLIGAR"}
ACTION_UNKNOWN = "DESLIGAR/REINICIAR"

DEFAULT_MESSAGES = collections.OrderedDict([
    ("boot",
     "🟢 O computador {host} foi LIGADO em {date} às {time}.\\n\\n"
     "Endereços IP:\\n{ips}"),
    ("shutdown",
     "🔴 O computador {host} vai {action} em {date} às {time}.\\n"
     "Ficou ligado por {uptime}."),
    ("unclean",
     "⚠️ O computador {host} NÃO foi desligado corretamente.\\n"
     "A sessão iniciada em {last_boot} terminou sem um desligamento normal "
     "(queda de energia, travamento ou desligamento forçado)."),
    ("started",
     "▶️ Monitoramento do bootbell ATIVADO no computador {host} "
     "em {date} às {time}.\\nO computador está ligado há {uptime}.\\n\\n"
     "Endereços IP:\\n{ips}"),
    ("stopped",
     "⏸️ Monitoramento do bootbell DESATIVADO no computador {host} "
     "em {date} às {time}.\\nO computador continua ligado (há {uptime})."),
    ("test",
     "🔔 Teste do bootbell no computador {host} em {date} às {time}: "
     "as notificações estão funcionando!"),
    ("delayed",
     "📨 Esta mensagem não pôde ser enviada na hora e foi entregue com atraso."),
])

DEFAULTS = collections.OrderedDict([
    ("general", collections.OrderedDict([
        ("hostname", ""),
        ("date_format", "%d/%m/%Y"),
        ("time_format", "%H:%M:%S"),
        ("ignore_interfaces", "docker* veth* br-* virbr* lxcbr* lxdbr*"),
        ("ipv4_only", "no"),
        ("request_timeout", "10"),
        ("boot_retry_for", "300"),
        ("ip_wait", "60"),
        ("shutdown_timeout", "20"),
    ])),
    ("telegram", collections.OrderedDict([
        ("token", ""),
        ("chat_id", ""),
        ("silent", "no"),
        ("api_url", "https://api.telegram.org"),
    ])),
    ("messages", DEFAULT_MESSAGES),
])


# --------------------------------------------------------------------------
# Saída
# --------------------------------------------------------------------------

def _write(stream, text):
    """Escreve sempre em UTF-8, mesmo sem locale configurado.

    Sob o systemd o Python 3.6 pode iniciar com codificação ASCII; escrever
    os bytes diretamente evita UnicodeEncodeError com acentos e emojis.
    """
    data = (text + "\n").encode("utf-8", "replace")
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        stream.flush()
        buffer.write(data)
        buffer.flush()
    else:  # pragma: no cover - streams sem buffer (ex.: testes)
        stream.write(text + "\n")
        stream.flush()


def log(message):
    _write(sys.stderr, message)


def log_error(message):
    log("erro: {}".format(message))


def out(message=""):
    _write(sys.stdout, message)


# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------

class ConfigError(Exception):
    pass


def _positive_float(section, key):
    try:
        value = section.getfloat(key)
    except ValueError:
        raise ConfigError("[{}] {} deve ser um número".format(section.name, key))
    if value <= 0:
        raise ConfigError("[{}] {} deve ser maior que zero".format(section.name, key))
    return value


def _non_negative_float(section, key):
    try:
        value = section.getfloat(key)
    except ValueError:
        raise ConfigError("[{}] {} deve ser um número".format(section.name, key))
    if value < 0:
        raise ConfigError("[{}] {} não pode ser negativo".format(section.name, key))
    return value


def _boolean(section, key):
    try:
        return section.getboolean(key)
    except ValueError:
        raise ConfigError("[{}] {} deve ser yes ou no".format(section.name, key))


class Config(object):
    """Configuração lida de um arquivo INI."""

    def __init__(self, path):
        self.path = path
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_dict(DEFAULTS)
        try:
            with open(path, encoding="utf-8") as handle:
                parser.read_file(handle)
        except FileNotFoundError:
            raise ConfigError("arquivo de configuração não encontrado: {}".format(path))
        except PermissionError:
            raise ConfigError(
                "sem permissão para ler {} (tente novamente com sudo)".format(path))
        except (configparser.Error, UnicodeDecodeError) as exc:
            raise ConfigError("erro ao ler {}: {}".format(path, exc))

        general = parser["general"]
        self.hostname = general.get("hostname").strip() or socket.gethostname()
        self.date_format = general.get("date_format")
        self.time_format = general.get("time_format")
        self.ignore_interfaces = general.get("ignore_interfaces").split()
        self.ipv4_only = _boolean(general, "ipv4_only")
        self.request_timeout = _positive_float(general, "request_timeout")
        self.boot_retry_for = _positive_float(general, "boot_retry_for")
        self.ip_wait = _non_negative_float(general, "ip_wait")
        self.shutdown_timeout = _positive_float(general, "shutdown_timeout")

        telegram = parser["telegram"]
        token = telegram.get("token").strip()
        # Aceita o token também no formato "bot123:ABC" (como na URL da API).
        if token.startswith("bot"):
            token = token[3:]
        self.token = token
        self.chat_id = telegram.get("chat_id").strip()
        self.silent = _boolean(telegram, "silent")
        self.api_url = telegram.get("api_url").strip().rstrip("/")

        self.messages = dict(DEFAULT_MESSAGES)
        for key, value in parser["messages"].items():
            self.messages[key] = value

    def validate(self):
        problems = []
        if not self.token:
            problems.append("[telegram] token não foi configurado")
        elif ":" not in self.token:
            problems.append("[telegram] token parece inválido (formato esperado: 123456:ABC...)")
        if not self.chat_id:
            problems.append("[telegram] chat_id não foi configurado")
        if problems:
            raise ConfigError("{}: {}".format(self.path, "; ".join(problems)))

    def masked_token(self):
        if not self.token:
            return "(não configurado)"
        return "configurado (bot {}, segredo oculto)".format(self.token.split(":")[0])


# --------------------------------------------------------------------------
# Envio de mensagens
# --------------------------------------------------------------------------

class SendError(Exception):
    def __init__(self, message, permanent=False):
        Exception.__init__(self, message)
        self.permanent = permanent


@contextlib.contextmanager
def _address_family(ipv4_only):
    """Se pedido, força as conexões a usarem somente IPv4."""
    if not ipv4_only:
        yield
        return
    original = socket.getaddrinfo

    def getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
        return original(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = getaddrinfo_ipv4
    try:
        yield
    finally:
        socket.getaddrinfo = original


def _telegram_description(raw):
    try:
        return json.loads(raw.decode("utf-8")).get("description")
    except Exception:
        return None


class TelegramNotifier(object):
    """Envia mensagens pela Bot API do Telegram.

    Novos canais (ntfy, webhook, e-mail...) podem ser adicionados criando
    classes com o mesmo método send(text, timeout).
    """

    def __init__(self, config):
        self.config = config

    def send(self, text, timeout):
        config = self.config
        # Atenção: a URL contém o token. Nunca inclua a URL em logs.
        url = "{}/bot{}/sendMessage".format(config.api_url, config.token)
        payload = {
            "chat_id": config.chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
        if config.silent:
            payload["disable_notification"] = "true"
        request = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(payload).encode("utf-8"),
            headers={"User-Agent": "bootbell/{}".format(__version__)},
        )
        try:
            with _address_family(config.ipv4_only):
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    body = response.read()
        except urllib.error.HTTPError as exc:
            description = _telegram_description(exc.read()) or exc.reason
            permanent = 400 <= exc.code < 500 and exc.code != 429
            raise SendError("o Telegram respondeu HTTP {}: {}".format(exc.code, description),
                            permanent)
        except urllib.error.URLError as exc:
            raise SendError("falha de rede: {}".format(exc.reason))
        except (socket.timeout, OSError) as exc:
            raise SendError("falha de rede: {}".format(exc))

        try:
            result = json.loads(body.decode("utf-8"))
        except ValueError:
            raise SendError("resposta inválida do Telegram")
        if not result.get("ok"):
            raise SendError("o Telegram recusou a mensagem: {}".format(
                result.get("description")), permanent=True)


def send_with_retry(notifier, text, deadline, request_timeout):
    """Tenta enviar até conseguir, até um erro permanente ou até o prazo."""
    delay = RETRY_INITIAL_DELAY
    attempt = 0
    while True:
        attempt += 1
        remaining = deadline - time.monotonic()
        timeout = max(1.0, min(request_timeout, remaining))
        try:
            notifier.send(text, timeout)
            return
        except SendError as exc:
            remaining = deadline - time.monotonic()
            if exc.permanent or remaining <= delay:
                raise
            log("tentativa {} falhou ({}); tentando de novo em {:g}s".format(
                attempt, exc, delay))
            time.sleep(delay)
            delay = min(delay * 2, RETRY_MAX_DELAY)


# --------------------------------------------------------------------------
# Estado persistente (/var/lib/bootbell)
# --------------------------------------------------------------------------

class State(object):
    def __init__(self, directory):
        self.directory = directory

    def _path(self, name):
        return os.path.join(self.directory, name)

    def _read(self, name, default):
        try:
            with open(self._path(name), encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return default
        except (OSError, ValueError) as exc:
            log("aviso: ignorando {} ilegível: {}".format(self._path(name), exc))
            return default

    def _write(self, name, data):
        os.makedirs(self.directory, exist_ok=True)
        path = self._path(name)
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def _remove(self, name):
        try:
            os.remove(self._path(name))
        except FileNotFoundError:
            pass

    def load_session(self):
        session = self._read(SESSION_FILE, None)
        return session if isinstance(session, dict) else None

    def save_session(self, session):
        self._write(SESSION_FILE, session)

    def clear_session(self):
        self._remove(SESSION_FILE)

    def load_queue(self):
        queue = self._read(QUEUE_FILE, [])
        if not isinstance(queue, list):
            return []
        return [item for item in queue if isinstance(item, dict) and item.get("text")]

    def save_queue(self, queue):
        if queue:
            self._write(QUEUE_FILE, queue[-MAX_QUEUE:])
        else:
            self._remove(QUEUE_FILE)


# --------------------------------------------------------------------------
# Informações do sistema
# --------------------------------------------------------------------------

def _systemctl(*args):
    try:
        process = subprocess.run(
            ["systemctl"] + list(args),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            universal_newlines=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return process.stdout


def system_state():
    """Estado global do systemd: starting, running, stopping..."""
    output = _systemctl("is-system-running")
    return output.strip() if output else ""


def pending_shutdown_action():
    """Descobre se o systemd está reiniciando ou desligando a máquina."""
    output = _systemctl("list-jobs", "--no-legend", "--no-pager")
    if not output:
        return None
    units = set(output.split())
    if units.intersection(REBOOT_TARGETS):
        return "reboot"
    if units.intersection(POWEROFF_TARGETS):
        return "poweroff"
    return None


def boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def uptime_seconds():
    try:
        with open("/proc/uptime") as handle:
            return float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def is_booting():
    """True se o serviço foi iniciado pelo boot, False se manualmente."""
    state = system_state()
    if state in BOOTING_STATES:
        return True
    if state in RUNNING_STATES:
        return False
    uptime = uptime_seconds()
    return uptime is not None and uptime < BOOT_UPTIME_FALLBACK


def is_stopping():
    """True se o serviço está parando porque a máquina vai desligar."""
    state = system_state()
    if state == "stopping":
        return True
    if state in RUNNING_STATES or state in BOOTING_STATES:
        return False
    # Sem resposta do systemd: durante o desligamento isso é o mais provável.
    return True


def parse_ip_output(output, ignore_patterns):
    """Interpreta a saída de `ip -o addr show`.

    Retorna [(interface, endereço)], somente endereços de escopo global
    (exclui loopback e link-local) de interfaces não ignoradas.
    """
    addresses = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] not in ("inet", "inet6"):
            continue
        interface = parts[1].split("@")[0].rstrip(":")
        address = parts[3].split("/")[0]
        try:
            scope = parts[parts.index("scope") + 1]
        except (ValueError, IndexError):
            scope = ""
        if scope != "global":
            continue
        if any(fnmatch.fnmatch(interface, pattern) for pattern in ignore_patterns):
            continue
        addresses.append((interface, address))
    return addresses


def get_ip_addresses(ignore_patterns):
    for ip_command in ("ip", "/sbin/ip", "/usr/sbin/ip", "/bin/ip"):
        try:
            output = subprocess.check_output(
                [ip_command, "-o", "addr", "show"],
                stderr=subprocess.DEVNULL, universal_newlines=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        return parse_ip_output(output, ignore_patterns)
    return []


def has_ipv4(addresses):
    return any(":" not in address for _, address in addresses)


def wait_for_ipv4(config):
    """No boot, espera até config.ip_wait segundos por um IPv4 global.

    O network-online.target pode ser atingido antes do DHCP terminar (ex.:
    IPv6 fixo e IPv4 por DHCP no NetworkManager com ipv4.may-fail=yes). Sem
    esta espera, o aviso de "ligado" sairia sem o endereço IPv4.
    Retorna a lista de endereços encontrada por último.
    """
    deadline = time.monotonic() + config.ip_wait
    addresses = get_ip_addresses(config.ignore_interfaces)
    if has_ipv4(addresses) or config.ip_wait <= 0:
        return addresses
    log("aguardando um endereço IPv4 (até {:g}s)".format(config.ip_wait))
    while time.monotonic() < deadline:
        time.sleep(min(IP_WAIT_INTERVAL, max(0.0, deadline - time.monotonic())))
        addresses = get_ip_addresses(config.ignore_interfaces)
        if has_ipv4(addresses):
            return addresses
    log("nenhum endereço IPv4 apareceu em {:g}s; seguindo sem ele".format(config.ip_wait))
    return addresses


# --------------------------------------------------------------------------
# Formatação
# --------------------------------------------------------------------------

def format_duration(seconds):
    if seconds is None:
        return "tempo desconhecido"
    minutes = int(seconds // 60)
    if minutes < 1:
        return "menos de um minuto"
    days, rest = divmod(minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    parts = []
    for value, singular, plural in ((days, "dia", "dias"),
                                    (hours, "hora", "horas"),
                                    (minutes, "minuto", "minutos")):
        if value:
            parts.append("{} {}".format(value, singular if value == 1 else plural))
    if len(parts) == 1:
        return parts[0]
    return "{} e {}".format(", ".join(parts[:-1]), parts[-1])


def format_ips(addresses):
    if not addresses:
        return "nenhum endereço encontrado"
    grouped = collections.OrderedDict()
    for interface, address in addresses:
        grouped.setdefault(interface, []).append(address)
    return "\n".join("• {}: {}".format(interface, ", ".join(values))
                     for interface, values in grouped.items())


class _Placeholders(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render(template, values):
    """Preenche {campos} do modelo; campos desconhecidos ficam como estão."""
    text = template.replace("\\n", "\n")
    try:
        return text.format_map(_Placeholders(values))
    except (ValueError, IndexError, AttributeError, KeyError):
        return text


def format_timestamp(config, timestamp):
    try:
        moment = time.localtime(float(timestamp))
    except (TypeError, ValueError, OverflowError, OSError):
        return "data desconhecida"
    return "{} às {}".format(time.strftime(config.date_format, moment),
                             time.strftime(config.time_format, moment))


def build_values(config, now=None, addresses=None):
    now = time.time() if now is None else now
    if addresses is None:
        addresses = get_ip_addresses(config.ignore_interfaces)
    moment = time.localtime(now)
    return {
        "host": config.hostname,
        "date": time.strftime(config.date_format, moment),
        "time": time.strftime(config.time_format, moment),
        "uptime": format_duration(uptime_seconds()),
        "ips": format_ips(addresses),
        "version": __version__,
    }


# --------------------------------------------------------------------------
# Entrega com fila
# --------------------------------------------------------------------------

def deliver(config, state, notifier, items, deadline):
    """Envia mensagens em ordem; o que falhar vai para a fila.

    items: lista de (texto, atrasada). Mensagens atrasadas recebem a nota
    [messages] delayed no final.
    """
    queue = []
    gave_up = False
    for text, delayed in items:
        if not gave_up:
            final_text = text
            if delayed:
                final_text = "{}\n\n{}".format(text, render(config.messages["delayed"], {}))
            try:
                send_with_retry(notifier, final_text, deadline, config.request_timeout)
                continue
            except SendError as exc:
                log_error("não foi possível enviar a mensagem: {}".format(exc))
                gave_up = True
        queue.append({"text": text, "created": time.time()})
    if queue:
        log("{} mensagem(ns) guardada(s) para envio no próximo boot".format(len(queue)))
        try:
            state.save_queue(state.load_queue() + queue)
        except OSError as exc:
            log_error("não foi possível salvar a fila em {}: {}".format(state.directory, exc))
    return not queue


def print_messages(texts):
    for index, text in enumerate(texts):
        if index:
            out("-" * 40)
        out(text)


# --------------------------------------------------------------------------
# Comandos
# --------------------------------------------------------------------------

def cmd_boot(config, state, notifier, dry_run):
    booting = is_booting()
    addresses = None
    if booting and not dry_run:
        addresses = wait_for_ipv4(config)
    values = build_values(config, addresses=addresses)
    current_boot = boot_id()
    previous = state.load_session()

    items = [(item["text"], True) for item in state.load_queue()]
    if previous and previous.get("boot_id") and previous.get("boot_id") != current_boot:
        unclean_values = dict(values)
        unclean_values["last_boot"] = format_timestamp(config, previous.get("boot_time"))
        items.append((render(config.messages["unclean"], unclean_values), False))
    items.append((render(config.messages["boot" if booting else "started"], values), False))

    if dry_run:
        print_messages([text for text, _ in items])
        return 0

    now = time.time()
    uptime = uptime_seconds()
    state.save_queue([])
    state.save_session({
        "boot_id": current_boot,
        "boot_time": now - uptime if uptime is not None else now,
        "started": now,
        "version": __version__,
    })
    log("{}: enviando {} mensagem(ns)".format(
        "boot detectado" if booting else "monitoramento ativado", len(items)))
    if deliver(config, state, notifier, items,
               time.monotonic() + config.boot_retry_for):
        log("mensagens enviadas")
    return 0


def cmd_shutdown(config, state, notifier, dry_run):
    values = build_values(config)
    if is_stopping():
        values["action"] = ACTION_NAMES.get(pending_shutdown_action(), ACTION_UNKNOWN)
        text = render(config.messages["shutdown"], values)
        event = "sistema vai {}".format(values["action"])
    else:
        text = render(config.messages["stopped"], values)
        event = "monitoramento desativado"

    if dry_run:
        print_messages([text])
        return 0

    log("{}: enviando aviso".format(event))
    if deliver(config, state, notifier, [(text, False)],
               time.monotonic() + config.shutdown_timeout):
        log("aviso enviado")
    return 0


def cmd_test(config, state, notifier, dry_run):
    text = render(config.messages["test"], build_values(config))
    if dry_run:
        print_messages([text])
        return 0
    try:
        send_with_retry(notifier, text, time.monotonic() + config.request_timeout,
                        config.request_timeout)
    except SendError as exc:
        log_error("o envio falhou: {}".format(exc))
        return 1
    out("✅ Mensagem de teste enviada para o chat {}.".format(config.chat_id))
    return 0


def cmd_check(config, state, notifier, dry_run):
    status = 0
    out("Arquivo de configuração: {}".format(config.path))
    try:
        config.validate()
        out("Configuração: OK")
    except ConfigError as exc:
        out("Configuração: PROBLEMA - {}".format(exc))
        status = 1
    try:
        mode = os.stat(config.path).st_mode
        if mode & 0o004:
            out("Aviso: {} pode ser lido por qualquer usuário; ele contém o token "
                "do bot. Corrija com: chmod 640 {}".format(config.path, config.path))
    except OSError:
        pass
    out("Hostname: {}".format(config.hostname))
    out("Token: {}".format(config.masked_token()))
    out("Chat ID: {}".format(config.chat_id or "(não configurado)"))
    out("Somente IPv4: {}".format("sim" if config.ipv4_only else "não"))
    out("Interfaces ignoradas: {}".format(" ".join(config.ignore_interfaces) or "nenhuma"))
    out("Estado do systemd: {}".format(system_state() or "desconhecido"))

    values = build_values(config)
    values["action"] = ACTION_NAMES["reboot"]
    values["last_boot"] = format_timestamp(config, time.time() - 86400)
    out()
    out("Prévia das mensagens:")
    for key in ("boot", "shutdown", "unclean", "started", "stopped", "test", "delayed"):
        out()
        out("[{}]".format(key))
        out(render(config.messages[key], values))
    return status


COMMANDS = collections.OrderedDict([
    ("boot", (cmd_boot, "avisa que o computador ligou (usado pelo systemd)")),
    ("shutdown", (cmd_shutdown, "avisa que o computador vai desligar (usado pelo systemd)")),
    ("test", (cmd_test, "envia uma mensagem de teste")),
    ("check", (cmd_check, "valida a configuração e mostra uma prévia das mensagens")),
])


def build_parser():
    parser = argparse.ArgumentParser(
        prog="bootbell",
        description="Avisa no Telegram quando o computador liga ou desliga.")
    parser.add_argument("-V", "--version", action="version",
                        version="%(prog)s {}".format(__version__))
    parser.add_argument("-c", "--config", default=os.environ.get("BOOTBELL_CONFIG", DEFAULT_CONFIG),
                        help="arquivo de configuração (padrão: %(default)s)")
    parser.add_argument("--state-dir",
                        default=os.environ.get("BOOTBELL_STATE_DIR", DEFAULT_STATE_DIR),
                        help="diretório de estado (padrão: %(default)s)")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="mostra as mensagens em vez de enviá-las e não altera o estado")
    subparsers = parser.add_subparsers(dest="command", metavar="COMANDO")
    for name, (_, help_text) in COMMANDS.items():
        subparsers.add_parser(name, help=help_text, description=help_text)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(sys.stderr)
        return 2

    state = State(args.state_dir)
    if args.command == "shutdown" and not args.dry_run:
        # Desligamento limpo: remove o marcador antes de qualquer coisa que
        # possa travar, para o próximo boot não acusar desligamento incorreto.
        try:
            state.clear_session()
        except OSError as exc:
            log_error("não foi possível limpar {}: {}".format(state.directory, exc))

    try:
        config = Config(args.config)
        if args.command != "check":
            config.validate()
    except ConfigError as exc:
        log_error(str(exc))
        # Nunca falha no desligamento, para não atrapalhar o shutdown.
        return 0 if args.command == "shutdown" else 1

    handler = COMMANDS[args.command][0]
    try:
        return handler(config, state, TelegramNotifier(config), args.dry_run)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # Saída cortada (ex.: "bootbell check | head"): sai em silêncio.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0 if args.command == "shutdown" else 1
    except Exception as exc:  # pragma: no cover - rede de segurança
        log_error("erro inesperado: {}: {}".format(type(exc).__name__, exc))
        return 0 if args.command == "shutdown" else 1


if __name__ == "__main__":
    sys.exit(main())
