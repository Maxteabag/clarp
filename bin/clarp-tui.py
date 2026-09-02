#!/usr/bin/env python3
"""Textual setup, configuration, pairing, and diagnostics for Clarp."""
from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
import urllib.parse

import qrcode
from textual import work
from textual.app import App, ComposeResult
from textual.containers import (
    Container, Horizontal, ScrollableContainer, VerticalScroll,
)
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import (
    Button, Footer, Header, Input, LoadingIndicator, RichLog, Select,
    SelectionList, Static, TabbedContent, TabPane,
)


def admin_script() -> Path:
    return Path(__file__).with_name("clarp-admin.py")


CLARP_THEME = Theme(
    name="clarp",
    primary="#97CB93",
    secondary="#6D8DC4",
    accent="#6D8DC4",
    warning="#f59e0b",
    error="#BE728C",
    success="#4ADE80",
    foreground="#a9b1d6",
    background="#1A1B26",
    surface="#24283B",
    panel="#414868",
    dark=True,
    variables={
        "border": "#414868",
        "footer-background": "#24283B",
        "footer-key-foreground": "#7FA1DE",
        "button-color-foreground": "#1A1B26",
        "input-selection-background": "#2a3144 40%",
    },
)


def share_dir() -> Path:
    configured = os.environ.get("CLARP_SHARE_DIR")
    if configured:
        return Path(configured)
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Clarp"
    return Path(os.environ.get(
        "XDG_DATA_HOME", Path.home() / ".local/share")) / "clarp"


def server_is_installed() -> bool:
    root = share_dir()
    if os.environ.get("CLARP_DEPLOYMENT_MODE") == "container":
        return (root / "server.py").is_file()
    return (root / "current/server.py").is_file()


def config_dir() -> Path:
    configured = os.environ.get("CLARP_CONFIG_DIR")
    if configured:
        return Path(configured)
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Clarp"
    return Path(os.environ.get(
        "XDG_CONFIG_HOME", Path.home() / ".config")) / "clarp"


def installed_setup_state() -> dict:
    """Read reconfiguration defaults without exposing stored API keys."""
    state: dict = {}
    install_path = config_dir() / "install.json"
    config_path = config_dir() / "config.toml"
    try:
        installed = json.loads(install_path.read_text())
    except (OSError, json.JSONDecodeError):
        installed = {}
    try:
        configured = tomllib.loads(config_path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        configured = {}
    backend = str(installed.get("backend") or "both")
    state["backends"] = ({"claude", "codex"} if backend == "both"
                         else {backend} & {"claude", "codex"})
    state["toolchain"] = str(installed.get("toolchain") or "managed")
    state["skills"] = (set(installed["skills"])
                       if isinstance(installed.get("skills"), list) else None)
    tts = configured.get("tts", {}) if isinstance(configured, dict) else {}
    network = configured.get("network", {}) if isinstance(configured, dict) else {}
    server = configured.get("server", {}) if isinstance(configured, dict) else {}
    whisper = configured.get("whisper", {}) if isinstance(configured, dict) else {}
    if whisper and not bool(whisper.get("enabled", True)):
        state["transcription"] = "none"
    elif whisper.get("provider") and whisper.get("model"):
        state["transcription"] = (
            f"{whisper['provider']}:{whisper['model']}")
    else:
        state["transcription"] = str(
            installed.get("transcription") or "recommended")
    state["tts"] = str(tts.get("provider") or "cartesia")
    state["tts-fallback"] = str(tts.get("fallback") or "none")
    state["network"] = str(network.get("mode") or "off")
    state["public-url"] = str(server.get("public_base_url") or "https://")
    return state


def optional_skill_selections(
    selected: set[str] | None = None,
) -> list[tuple[str, str, bool]]:
    manifest_path = admin_script().parents[1] / "skills/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    choices = []
    for item in manifest["skills"]:
        pack = str(item.get("pack", ""))
        if not (pack in {"native", "messaging"}
                or item.get("default_enabled") is False):
            continue
        skill_id = str(item["id"])
        name = skill_id.removeprefix("clarp-").replace("-", " ").title()
        enabled = (skill_id in selected if selected is not None else
                   skill_id in {"clarp-calendar", "clarp-location"})
        choices.append((f"{name} ({pack})", skill_id, enabled))
    return choices


def transcription_selections(current: str) -> list[tuple[str, str]]:
    choices = [
        ("Recommended local model", "recommended"),
        ("No server model", "none"),
    ]
    if current not in {value for _label, value in choices}:
        choices.insert(0, (f"Current model ({current})", current))
    return choices


def tts_selections(current: str, *, fallback: bool = False) -> list[tuple[str, str]]:
    remote = [
        ("Cartesia", "cartesia"),
        ("ElevenLabs", "elevenlabs"),
        ("Deepgram Flux", "deepgram"),
    ]
    choices = ([("No fallback", "none"), *remote] if fallback else [
        *remote, ("No voice", "none")])
    if current not in {value for _label, value in choices}:
        choices.insert(0, (f"Current legacy/custom provider ({current})", current))
    return choices


def included_skill_selections() -> list[tuple[str, str, bool]]:
    manifest_path = admin_script().parents[1] / "skills/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    choices = []
    for item in manifest["skills"]:
        pack = str(item.get("pack", ""))
        included = pack == "core" or (
            pack == "artifacts" and item.get("default_enabled") is not False)
        if not included:
            continue
        skill_id = str(item["id"])
        name = skill_id.removeprefix("clarp-").replace("-", " ").title()
        choices.append((f"{name} ({pack})", skill_id, True))
    return choices


def run_admin_stream(
    arguments: list[str], input_text: str, on_line: Callable[[str], None],
) -> int:
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [sys.executable, str(admin_script()), *arguments],
        stdin=subprocess.PIPE if input_text else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=environment)
    if input_text:
        assert process.stdin is not None
        process.stdin.write(input_text)
        process.stdin.close()
    assert process.stdout is not None
    for line in process.stdout:
        on_line(line.rstrip())
    return process.wait()


def setup_admin_command(values: dict) -> list[str]:
    command = [
        "setup", "--non-interactive",
        "--backend", str(values["backend"]),
        "--toolchain", str(values["toolchain"]),
        "--transcription", str(values["transcription"]),
        "--tts", str(values["tts"]),
        "--tts-fallback", str(values["tts-fallback"]),
    ]
    for skill_id in values["optional-skills"]:
        command += ["--optional-skill", str(skill_id)]
    return command


def network_admin_command(values: dict) -> list[str]:
    command = ["network", "use", str(values["network"])]
    if values["network"] == "manual":
        command += ["--url", str(values["public-url"])]
    return command


def render_pairing_qr(uri: str) -> str:
    qr = qrcode.QRCode(
        border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(uri)
    qr.make(fit=True)
    # Render every module as a 2x1 terminal-cell block. Typical terminal cells
    # are twice as tall as they are wide, so this preserves a square QR instead
    # of stretching the half-row ASCII representation vertically.
    return "\n".join(
        "".join("██" if module else "  " for module in row)
        for row in qr.get_matrix()) + "\n"


class PairingQRScreen(ModalScreen[None]):
    BINDINGS = [("escape", "close", "Close")]
    CSS = """
    PairingQRScreen { align: center middle; background: transparent; }
    #pair-qr-dialog {
        width: 90; max-width: 95%; height: 90%; padding: 1 2;
        background: $surface; color: $text; border: round $primary;
        overflow-x: auto; overflow-y: auto;
        border-title-align: left;
        border-title-color: $primary;
        border-title-background: $surface;
        border-title-style: bold;
        border-subtitle-align: right;
        border-subtitle-color: $primary;
        border-subtitle-background: $surface;
    }
    #pair-modal-title { color: $text-muted; margin-bottom: 1; }
    #pair-modal-qr {
        width: auto; height: auto; color: black; background: white;
        padding: 1 2;
    }
    #pair-modal-details { width: 100%; height: auto; margin: 1 0; }
    """

    def __init__(self, *, qr_text: str, uri: str, expires_at: int):
        super().__init__()
        self.qr_text = qr_text
        self.uri = uri
        self.expires_at = expires_at

    def compose(self) -> ComposeResult:
        dialog = ScrollableContainer(id="pair-qr-dialog")
        dialog.border_title = "Pair iPhone"
        dialog.border_subtitle = "Close: [bold]<esc>[/]"
        with dialog:
            yield Static(
                "Scan this complete code with Clarp on iPhone",
                id="pair-modal-title")
            yield Static(self.qr_text, id="pair-modal-qr")
            yield Static(
                f"One use only. Expires at {self.expires_at}.\n\n{self.uri}",
                id="pair-modal-details")
            yield Button("Close", id="close-pair-qr", variant="primary")

    def action_close(self) -> None:
        self.dismiss()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close-pair-qr":
            self.dismiss()


class ClarpAdminApp(App[None]):
    TITLE = "Clarp"
    SUB_TITLE = "Computer setup and administration"
    CSS = """
    Screen { background: $background; color: $text; }
    Header { background: $surface; color: $primary; }
    TabbedContent { height: 1fr; }
    TabbedContent > ContentSwitcher { height: 1fr; }
    TabPane {
        height: 1fr; padding: 1 2; overflow-y: auto;
        align-horizontal: center;
    }
    TabPane > VerticalScroll { align-horizontal: center; }

    .form-page {
        width: 76; max-width: 100%; height: auto;
        margin: 0 1;
    }
    .form-section {
        height: auto;
        border: round $panel;
        background: $surface;
        padding: 1;
        margin-bottom: 1;
        border-title-align: left;
        border-title-color: $primary;
        border-title-background: $surface;
        border-title-style: bold;
    }
    .field-container {
        position: relative;
        height: auto;
        border: solid $panel;
        background: $surface;
        padding: 0;
        margin-top: 0;
        border-title-align: left;
        border-title-color: $text-muted;
        border-title-background: $surface;
        border-title-style: none;
    }
    .field-container:focus-within {
        border: solid $primary;
        border-title-color: $primary;
    }
    .field-container Input {
        width: 1fr; height: 1; border: none; padding: 0;
        background: $surface;
    }
    .field-container Input:focus {
        border: none; background-tint: $foreground 5%;
    }
    .field-container Select {
        width: 1fr; border: none; padding: 0; background: $surface;
    }
    .field-container SelectionList {
        width: 1fr; border: none; padding: 0; background: $surface;
    }
    .password-field-row { width: 100%; height: 1; }
    .password-field-row Input { width: 1fr; }
    .password-toggle-button {
        width: 6; min-width: 6; height: 1;
        border: none; margin-left: 1; padding: 0;
    }
    #backends { height: 4; }
    #included-skills { height: 20; }
    #optional-skills { height: 10; }
    .actions { height: auto; margin: 0 0 1 0; }
    Button { margin-right: 1; }
    #setup-progress { height: 2; margin-bottom: 1; }
    #setup-spinner { width: 5; height: 1; }
    #setup-status { width: 1fr; padding: 0 1; }
    #setup-status.running { color: $text-muted; }
    #setup-status.success { color: $success; }
    #setup-status.error { color: $error; }
    RichLog {
        border: solid $primary-darken-2;
        background: $surface-darken-1;
        padding: 0 1;
        scrollbar-size: 1 1;
    }
    #setup-log { height: 16; }
    #overview-log { height: 1fr; min-height: 12; }
    #pair-log { height: 8; }
    #pair-network-status, #pair-summary {
        height: auto; color: $text-muted; margin-bottom: 1;
    }
    #pair-network-status.error { color: $error; }
    .hint { color: $text-muted; margin-bottom: 1; }
    .initially-hidden { display: none; }
    """

    def __init__(self, *, first_run: bool = False):
        super().__init__()
        self.register_theme(CLARP_THEME)
        self.theme = "clarp"
        self.first_run = first_run
        self.server_installed = server_is_installed()
        self.setup_state = installed_setup_state() if self.server_installed else {}
        self.network_mode = "off"
        self.last_pairing_qr: tuple[str, str, int] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="setup" if self.first_run else "overview"):
            with TabPane("Overview", id="overview"):
                with Container(classes="form-page"):
                    with Container(classes="form-section") as overview_section:
                        overview_section.border_title = "Computer health"
                        yield Static(
                            "The TUI calls the same clarp-admin commands used by "
                            "automation.", classes="hint")
                        with Horizontal(classes="actions"):
                            yield Button(
                                "Refresh doctor", id="doctor", variant="primary")
                            yield Button("Show paths", id="paths")
                        yield RichLog(
                            id="overview-log", wrap=True, markup=False)
            with TabPane("Setup", id="setup"):
                with VerticalScroll():
                    with Container(classes="form-page"):
                        yield Static(
                            "Managed tools and recommended transcription are the "
                            "safest defaults.", classes="hint")
                        with Container(classes="form-section") as agent_section:
                            agent_section.border_title = "Agents"
                            yield self._selection_list(
                                "Backends", "backends", [
                                    ("Claude", "claude", "claude" in self.setup_state.get("backends", {"claude", "codex"})),
                                    ("Codex", "codex", "codex" in self.setup_state.get("backends", {"claude", "codex"})),
                                ])
                            yield self._select("Tools", "toolchain", [
                                ("Managed pinned tools", "managed"),
                                ("Existing PATH commands", "existing"),
                                ("Configure later", "none")],
                                self.setup_state.get("toolchain", "managed"))
                            yield self._select(
                                "Voice transcription", "transcription",
                                transcription_selections(self.setup_state.get(
                                    "transcription", "recommended")),
                                self.setup_state.get("transcription", "recommended"))
                        with Container(classes="form-section") as voice_section:
                            voice_section.border_title = "Voice output"
                            yield self._select("Provider", "tts",
                                tts_selections(self.setup_state.get(
                                    "tts", "cartesia")),
                                self.setup_state.get("tts", "cartesia"))
                            yield self._select("Fallback", "tts-fallback",
                                tts_selections(self.setup_state.get(
                                    "tts-fallback", "none"), fallback=True),
                                self.setup_state.get("tts-fallback", "none"))
                            yield self._input(
                                "Cartesia API key", "cartesia-key", "",
                                password=True, field_id="cartesia-key-field")
                            yield self._input(
                                "ElevenLabs API key", "eleven-key", "",
                                password=True, field_id="eleven-key-field",
                                hidden=True)
                            yield self._input(
                                "Deepgram API key", "deepgram-key", "",
                                password=True, field_id="deepgram-key-field",
                                hidden=True)
                        with Container(classes="form-section") as phone_section:
                            phone_section.border_title = "Phone access"
                            yield self._select("Network", "network", [
                                ("Tailscale Serve", "tailscale"),
                                ("Same LAN + Bonjour", "lan"),
                                ("Existing HTTPS URL", "manual"),
                                ("Local only", "off")],
                                self.setup_state.get("network", "tailscale"))
                            yield self._input(
                                "Manual HTTPS URL", "public-url",
                                self.setup_state.get("public-url", "https://"),
                                field_id="manual-url-field", hidden=True)
                        with Container(classes="form-section") as skill_section:
                            skill_section.border_title = "Skills"
                            yield Static(
                                "Core and standard artifact skills are included "
                                "automatically. Additional integrations are optional.",
                                classes="hint")
                            yield self._selection_list(
                                "Included", "included-skills",
                                included_skill_selections(), disabled=True)
                            yield self._selection_list(
                                "Optional", "optional-skills",
                                optional_skill_selections(
                                    self.setup_state.get("skills")))
                        with Horizontal(classes="actions"):
                            yield Button(
                                "Install / apply", id="apply-setup",
                                variant="success")
                        with Horizontal(id="setup-progress"):
                            yield LoadingIndicator(
                                id="setup-spinner", classes="initially-hidden")
                            yield Static(
                                "Ready to install." if not self.server_installed
                                else "Ready to apply changes.", id="setup-status")
                        yield RichLog(id="setup-log", wrap=True, markup=False)
            with TabPane("Pair iPhone", id="pair"):
                with VerticalScroll():
                    with Container(classes="form-page"):
                        yield Static(
                            "Creates a one-use credential that expires after ten "
                            "minutes.", classes="hint")
                        with Container(classes="form-section") as network_section:
                            network_section.border_title = "Connection"
                            yield Static(
                                "Loading the configured phone network…",
                                id="pair-network-status")
                            yield self._input(
                                "Computer URL", "pair-url", "",
                                field_id="pair-url-field")
                        with Container(classes="form-section") as device_section:
                            device_section.border_title = "Device access"
                            yield self._input(
                                "Device name", "pair-name", "iPhone")
                            yield self._select("Access", "pair-scope", [
                                ("Full Computer administration", "full"),
                                ("Limited chat and read access", "limited")],
                                "full")
                        with Horizontal(classes="actions"):
                            yield Button(
                                "Generate QR", id="pair-create",
                                variant="primary", disabled=True)
                            yield Button(
                                "Show QR again", id="pair-show",
                                classes="initially-hidden")
                            yield Button("List paired devices", id="pair-list")
                        yield Static(
                            "No active pairing code.", id="pair-summary")
                        yield RichLog(id="pair-log", wrap=True, markup=False)
        yield Footer()

    @staticmethod
    def _select(label: str, widget_id: str, options, value: str) -> Container:
        container = Container(
            Select(
                options, value=value, allow_blank=False, compact=True,
                id=widget_id),
            id=f"container-{widget_id}", classes="field-container")
        container.border_title = label
        return container

    @staticmethod
    def _selection_list(label: str, widget_id: str,
                        selections: list[tuple[str, str, bool]],
                        disabled: bool = False) -> Container:
        container = Container(
            SelectionList(
                *selections, id=widget_id, compact=True, disabled=disabled),
            id=f"container-{widget_id}", classes="field-container")
        container.border_title = label
        return container

    @staticmethod
    def _input(label: str, widget_id: str, value: str,
               password: bool = False, field_id: str | None = None,
               hidden: bool = False) -> Container:
        input_widget = Input(
            value=value, password=password, id=widget_id)
        content = (
            Horizontal(
                input_widget,
                Button(
                    "Show", id=f"toggle-password-{widget_id}",
                    classes="password-toggle-button"),
                classes="password-field-row")
            if password else input_widget)
        container = Container(
            content,
            id=field_id or f"container-{widget_id}",
            classes=("field-container initially-hidden"
                     if hidden else "field-container"))
        container.border_title = label
        return container

    def _value(self, widget_id: str) -> str:
        widget = self.query_one(f"#{widget_id}")
        return str(getattr(widget, "value", "") or "")

    def _selected(self, widget_id: str) -> list[str]:
        return [str(value) for value in self.query_one(
            f"#{widget_id}", SelectionList).selected]

    def on_mount(self) -> None:
        self._sync_network_fields()
        self._sync_credential_fields()
        if not self.server_installed:
            tabs = self.query_one(TabbedContent)
            tabs.hide_tab("overview")
            tabs.hide_tab("pair")
            tabs.active = "setup"
            return
        self.run_admin(["doctor"], "overview-log")
        self.load_network_state()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "network":
            self._sync_network_fields()
        elif event.select.id in {"tts", "tts-fallback"}:
            self._sync_credential_fields()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "pair-url":
            self._sync_pair_button()

    def _sync_network_fields(self) -> None:
        self.query_one("#manual-url-field").display = (
            self._value("network") == "manual")

    def _sync_credential_fields(self) -> None:
        selected = {self._value("tts"), self._value("tts-fallback")}
        self.query_one("#cartesia-key-field").display = "cartesia" in selected
        self.query_one("#eleven-key-field").display = "elevenlabs" in selected
        self.query_one("#deepgram-key-field").display = "deepgram" in selected

    @staticmethod
    def _pair_url_error(mode: str, value: str) -> str:
        if mode == "off":
            return "Choose Tailscale, LAN, or Manual HTTPS in Setup first."
        parsed = urllib.parse.urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "A complete http:// or https:// Computer URL is required."
        if parsed.hostname in {"127.0.0.1", "::1", "localhost"}:
            return "The iPhone cannot pair through a loopback-only URL."
        if mode in {"tailscale", "manual"} and parsed.scheme != "https":
            return "Tailscale and manual pairing require an HTTPS URL."
        return ""

    def _sync_pair_button(self) -> None:
        error = self._pair_url_error(
            self.network_mode, self._value("pair-url"))
        self.query_one("#pair-create", Button).disabled = bool(error)

    @work(thread=True, exclusive=True, group="network-state")
    def load_network_state(self) -> None:
        result = subprocess.run([
            sys.executable, str(admin_script()), "network", "status",
        ], text=True, capture_output=True, check=False)
        if result.returncode != 0:
            self.call_from_thread(
                self._set_pair_network_error,
                (result.stderr or result.stdout).strip()
                or "Could not read the configured network.")
            return
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            self.call_from_thread(
                self._set_pair_network_error,
                "The network status response was invalid.")
            return
        self.call_from_thread(self._apply_network_state, payload)

    def _set_pair_network_error(self, message: str) -> None:
        self.network_mode = "off"
        status = self.query_one("#pair-network-status", Static)
        status.update(message)
        status.add_class("error")
        self.query_one("#pair-create", Button).disabled = True

    def _apply_network_state(self, payload: dict) -> None:
        mode = str(payload.get("mode") or "off")
        url = str(payload.get("pairing_url") or "").strip()
        auth = bool(payload.get("auth_configured"))
        self.network_mode = mode
        field = self.query_one("#pair-url-field")
        input_widget = self.query_one("#pair-url", Input)
        input_widget.value = url
        field.display = mode != "off"
        input_widget.disabled = mode != "manual"
        if mode == "tailscale":
            message = (
                "Tailscale Serve address loaded from this Computer."
                if url else "Tailscale is selected but has no reachable URL.")
        elif mode == "manual":
            message = "Confirm or edit the HTTPS address managed by your proxy."
        elif mode == "lan":
            message = (
                "LAN address loaded from the Bonjour hostname. The iPhone must "
                "be on the same network.")
        else:
            message = (
                "Pairing is unavailable in Local-only mode. Choose Tailscale, "
                "LAN, or Manual HTTPS in Setup.")
        if mode != "off" and not auth:
            message += " Server authentication must also be enabled."
        error = self._pair_url_error(mode, url)
        status = self.query_one("#pair-network-status", Static)
        status.update(message)
        status.remove_class("error")
        if error or not auth:
            status.add_class("error")
        self.query_one("#pair-create", Button).disabled = bool(error) or not auth

    def on_button_pressed(self, event: Button.Pressed) -> None:
        action = event.button.id
        if action and action.startswith("toggle-password-"):
            input_id = action.removeprefix("toggle-password-")
            input_widget = self.query_one(f"#{input_id}", Input)
            input_widget.password = not input_widget.password
            event.button.label = "Show" if input_widget.password else "Hide"
        elif action == "doctor":
            self.run_admin(["doctor"], "overview-log")
        elif action == "paths":
            self.run_admin(["paths"], "overview-log")
        elif action == "pair-list":
            self.run_admin(["pair", "list"], "pair-log")
        elif action == "pair-show":
            if self.last_pairing_qr is not None:
                qr_text, uri, expires_at = self.last_pairing_qr
                self.push_screen(PairingQRScreen(
                    qr_text=qr_text, uri=uri, expires_at=expires_at))
        elif action == "pair-create":
            error = self._pair_url_error(
                self.network_mode, self._value("pair-url"))
            if error:
                self._replace_log("pair-log", error)
                return
            self.create_pairing({
                "url": self._value("pair-url"),
                "name": self._value("pair-name"),
                "scope": self._value("pair-scope"),
            })
        elif action == "apply-setup":
            backends = set(self._selected("backends"))
            if not backends:
                self._set_setup_status(
                    "Select at least one agent backend.", "error")
                self._replace_log(
                    "setup-log", "Select at least one agent backend.")
                return
            backend = "both" if backends == {"claude", "codex"} else next(
                iter(backends))
            self._begin_setup()
            self.apply_setup({
                key: self._value(key) for key in (
                    "toolchain", "transcription", "tts",
                    "tts-fallback", "network", "public-url",
                    "cartesia-key", "eleven-key", "deepgram-key")
            } | {
                "backend": backend,
                "optional-skills": self._selected("optional-skills"),
            })

    @work(thread=True, exclusive=True, group="admin-command")
    def run_admin(self, arguments: list[str], log_id: str,
                  input_text: str = "") -> None:
        result = subprocess.run(
            [sys.executable, str(admin_script()), *arguments],
            input=input_text, text=True, capture_output=True, check=False)
        output = (result.stdout + result.stderr).strip() or f"exit {result.returncode}"
        self.call_from_thread(self._replace_log, log_id, output)

    def _replace_log(self, log_id: str, output: str) -> None:
        try:
            log = self.query_one(f"#{log_id}", RichLog)
        except NoMatches:
            return
        log.clear()
        log.write(output)

    def _append_setup_log(self, output: str) -> None:
        if output:
            self.query_one("#setup-log", RichLog).write(output)

    def _set_setup_status(self, message: str, state: str) -> None:
        status = self.query_one("#setup-status", Static)
        status.update(message)
        for name in ("running", "success", "error"):
            status.remove_class(name)
        status.add_class(state)

    def _begin_setup(self) -> None:
        button = self.query_one("#apply-setup", Button)
        button.disabled = True
        button.label = (
            "Applying changes…" if self.server_installed else "Installing…")
        self.query_one("#setup-spinner", LoadingIndicator).display = True
        self._set_setup_status(
            "Installation in progress — this can take several minutes.",
            "running")
        log = self.query_one("#setup-log", RichLog)
        log.clear()
        log.write("Starting Clarp setup…")

    def _finish_setup(self, success: bool) -> None:
        self.query_one("#setup-spinner", LoadingIndicator).display = False
        button = self.query_one("#apply-setup", Button)
        button.disabled = False
        if success:
            self._unlock_installed_tabs()
            button.label = "Apply changes"
            self._set_setup_status(
                "✓ Installation complete — Clarp is running.", "success")
            self._append_setup_log("Installation complete.")
        else:
            button.label = "Retry installation"
            self._set_setup_status(
                "Installation failed — review the log below and retry.",
                "error")

    def _stream_admin(self, arguments: list[str], input_text: str = "") -> int:
        return run_admin_stream(
            arguments, input_text,
            lambda line: self.call_from_thread(self._append_setup_log, line))

    @work(thread=True, exclusive=True, group="setup")
    def apply_setup(self, values: dict) -> None:
        cartesia = str(values["cartesia-key"])
        eleven = str(values["eleven-key"])
        deepgram = str(values["deepgram-key"])
        selected_voice_providers = {
            str(values["tts"]), str(values["tts-fallback"])}
        command = setup_admin_command(values)
        success = False
        try:
            self.call_from_thread(
                self._append_setup_log, "Installing the Clarp runtime and service…")
            returncode = self._stream_admin(command)
            if returncode == 0:
                for provider, key in (
                        ("cartesia", cartesia), ("elevenlabs", eleven),
                        ("deepgram", deepgram)):
                    if provider not in selected_voice_providers or not key:
                        continue
                    self.call_from_thread(
                        self._append_setup_log,
                        f"Configuring {provider} credentials…")
                    returncode = self._stream_admin(
                        ["tts", "configure", provider, "--stdin"], key + "\n")
                    if returncode != 0:
                        break
            if returncode == 0:
                self.call_from_thread(
                    self._append_setup_log, "Applying voice-provider selection…")
                returncode = self._stream_admin([
                    "tts", "use", str(values["tts"]),
                    "--fallback", str(values["tts-fallback"]),
                ])
            if returncode == 0:
                self.call_from_thread(
                    self._append_setup_log, "Applying phone-network selection…")
                returncode = self._stream_admin(network_admin_command(values))
            if returncode == 0:
                success = server_is_installed()
                if not success:
                    self.call_from_thread(
                        self._append_setup_log,
                        "Setup exited successfully but no installed server was found.")
        except Exception as error:  # noqa: BLE001 - surface worker failures in UI
            self.call_from_thread(
                self._append_setup_log, f"Setup error: {error}")
        finally:
            self.call_from_thread(self._finish_setup, success)

    def _unlock_installed_tabs(self) -> None:
        if not server_is_installed():
            return
        self.server_installed = True
        tabs = self.query_one(TabbedContent)
        tabs.show_tab("overview")
        tabs.show_tab("pair")
        self.run_admin(["doctor"], "overview-log")
        self.load_network_state()

    @work(thread=True, exclusive=True, group="pair")
    def create_pairing(self, values: dict) -> None:
        result = subprocess.run([
            sys.executable, str(admin_script()), "pair", "create", "--json",
            "--url", str(values["url"]),
            "--name", str(values["name"]),
            "--scope", str(values["scope"]),
        ], text=True, capture_output=True, check=False)
        if result.returncode != 0:
            self.call_from_thread(
                self._replace_log, "pair-log", result.stdout + result.stderr)
            return
        payload = json.loads(result.stdout)
        qr_text = render_pairing_qr(payload["uri"])
        self.call_from_thread(
            self._present_pairing_qr, qr_text, payload["uri"],
            int(payload["expires_at"]))

    def _present_pairing_qr(
        self, qr_text: str, uri: str, expires_at: int,
    ) -> None:
        self.last_pairing_qr = (qr_text, uri, expires_at)
        self.query_one("#pair-show", Button).display = True
        self.query_one("#pair-summary", Static).update(
            f"Pairing code ready. One use only; expires at {expires_at}.")
        self._replace_log(
            "pair-log", "QR generated. Scan it from Settings → Add Computer "
            "in the Clarp iPhone app.")
        self.push_screen(PairingQRScreen(
            qr_text=qr_text, uri=uri, expires_at=expires_at))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--first-run", action="store_true")
    args = parser.parse_args()
    ClarpAdminApp(first_run=args.first_run).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
