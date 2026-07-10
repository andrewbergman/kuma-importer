#!/usr/bin/env python3
from __future__ import annotations

"""
kuma-importer

Interactive CSV/TXT/manual importer for Uptime Kuma.

Features
--------
- CSV, TXT, and manual import modes
- Interactive shell menu with defaults profiles (clone defaults)
- Idempotent create / update / skip logic
- Dry-run / verify / audit mode
- Validation mode for CSV/TXT inputs
- Export current monitors to CSV
- Drift reporting and optional deletion of unmanaged monitors
- Optional deletion of selected or all monitors with explicit confirmation
- Output improvements: colour, progress counters, quiet mode, log file output
- Safety improvements: optional automatic backup before non-dry-run changes
- Export + verify combined command
"""

import argparse
import configparser
import csv
import getpass
import html
import inspect
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

try:
    from uptime_kuma_api import UptimeKumaApi
except Exception as exc1:
    try:
        from uptime_kuma_api2 import UptimeKumaApi  # type: ignore
    except Exception as exc2:
        print(
            "ERROR: Cannot import a compatible Uptime Kuma API package.\n"
            "Install the v2-compatible package in your virtual environment, e.g.:\n"
            "  python3 -m pip install --upgrade uptime-kuma-api2\n"
            f"Primary import error: {exc1}\nFallback import error: {exc2}",
            file=sys.stderr,
        )
        raise SystemExit(2)

DEFAULT_INTERVAL = 60
DEFAULT_MAX_RETRIES = 1
DEFAULT_RETRY_INTERVAL = 60
DEFAULT_TYPE = "http"
DEFAULT_TIMEOUT = 48
DEFAULT_SOURCE_TAG = "Source:VPS"
DEFAULT_SERVICE_TAG = "Service:Website"
DEFAULT_ENV_TAG = "Environment:Production"
DEFAULT_CONFIG_PATH = "kuma_importer.conf"
DEFAULT_PROFILES_PATH = "kuma_defaults.conf"
DEFAULT_BACKUP_DIR = "backups"


@dataclass
class RuntimeOptions:
    url: str = ""
    username: str = ""
    password: str = ""
    default_client: str = "Clients"
    default_csv: str = "example_monitors.csv"
    default_txt: str = "example_domains.txt"
    default_export: str = "exported_monitors.csv"
    dry_run_default: bool = True
    verbose_default: bool = False
    auto_backup_default: bool = True


@dataclass
class Spec:
    client: str
    name: str
    url: str
    mtype: str
    interval: int = DEFAULT_INTERVAL
    maxretries: int = DEFAULT_MAX_RETRIES
    retry_interval: int = DEFAULT_RETRY_INTERVAL
    group: str = "Clients"
    tags: List[str] = field(default_factory=list)
    enabled: bool = True
    notifications: List[str] = field(default_factory=list)
    status_page: str = ""
    public_group: str = ""
    method: str = "GET"
    ignore_tls: bool = False
    timeout: int = DEFAULT_TIMEOUT
    accepted_statuscodes: List[str] = field(default_factory=lambda: ["200-299"])
    keyword: str = ""
    expected_value: str = ""


class Summary:
    def __init__(self) -> None:
        self.created = 0
        self.updated = 0
        self.skipped = 0
        self.failed = 0
        self.deleted = 0
        self.unmanaged = 0
        self.would_create = 0
        self.would_update = 0
        self.would_skip = 0
        self.would_delete = 0
        self.total = 0


class Console:
    def __init__(self, quiet: bool = False, colour: bool = True, log_file: str = "") -> None:
        self.quiet = quiet
        self.colour = colour and sys.stdout.isatty() and (os.getenv("NO_COLOR") is None)
        self.log_path = log_file.strip()
        self.log_handle = open(self.log_path, "a", encoding="utf-8") if self.log_path else None
        self.palette = {
            "create": "\033[32m",
            "update": "\033[33m",
            "skip": "\033[90m",
            "error": "\033[31m",
            "warn": "\033[35m",
            "info": "\033[36m",
            "delete": "\033[31m",
            "success": "\033[32m",
            "reset": "\033[0m",
        }

    def _paint(self, text: str, kind: str) -> str:
        if not self.colour or kind not in self.palette:
            return text
        return self.palette[kind] + text + self.palette["reset"]

    def _write_log(self, text: str) -> None:
        if self.log_handle:
            self.log_handle.write(text + "\n")
            self.log_handle.flush()

    def line(self, text: str = "", kind: str = "info", force: bool = False) -> None:
        clean = html.unescape(text)
        self._write_log(clean)
        if self.quiet and not force:
            return
        print(self._paint(clean, kind))

    def warn(self, text: str) -> None:
        clean = html.unescape(text)
        self._write_log(clean)
        print(self._paint(clean, "warn"), file=sys.stderr)

    def error(self, text: str) -> None:
        clean = html.unescape(text)
        self._write_log(clean)
        print(self._paint(clean, "error"), file=sys.stderr)

    def close(self) -> None:
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None


# ----------------------------- General helpers -----------------------------
def prompt_env(name: str, prompt: str, secret: bool = False) -> str:
    value = os.getenv(name)
    if value:
        return value
    return getpass.getpass(prompt) if secret else input(prompt).strip()


def parse_bool(value: Optional[str], default: bool = True) -> bool:
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def yes_no(prompt: str, default: bool = False) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    ans = input(prompt + suffix).strip().lower()
    if not ans:
        return default
    return ans in {"y", "yes"}


def parse_tags(value: str) -> List[str]:
    if not value:
        return []
    out: List[str] = []
    for chunk in value.replace(";", ",").split(","):
        item = chunk.strip()
        if item and item not in out:
            out.append(item)
    return out


def parse_list(value: str) -> List[str]:
    return parse_tags(value)


def normalise_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return raw
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        raw = "https://" + raw
    p = urlparse(raw)
    scheme = (p.scheme or "https").lower()
    netloc = p.netloc.lower()
    path = p.path or ""
    if path == "/":
        path = ""
    return urlunparse((scheme, netloc, path, "", "", ""))


def detect_client(domain_or_url: str) -> str:
    u = normalise_url(domain_or_url)
    host = urlparse(u).netloc or u
    if "DEMO-CLIENT" in host:
        return "DEMO-CLIENT"
    if host.endswith("Initech.com") or host == "Initech.com":
        return "Initech"
    if "Contoso" in host:
        return "Contoso"
    return "Clients"


def merge_tags(client: str, group: str, extras: Iterable[str]) -> List[str]:
    baseline = [
        f"Client:{client}",
        f"Group:{group}",
        DEFAULT_SOURCE_TAG,
        DEFAULT_SERVICE_TAG,
        DEFAULT_ENV_TAG,
    ]
    result: List[str] = []
    for item in baseline + list(extras):
        if item and item not in result:
            result.append(item)
    return result


def spec_key(spec: Spec) -> Tuple[str, str]:
    return (spec.mtype, spec.url)


def timestamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d-%H%M%S")


def ensure_dir(path: str) -> None:
    if path and not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


def apply_filters(specs: List[Spec], client: Optional[str], limit: Optional[int]) -> List[Spec]:
    if client:
        specs = [s for s in specs if s.client.lower() == client.lower()]
    if limit is not None and limit >= 0:
        specs = specs[:limit]
    return specs


def selected_match(m: dict, selectors: List[str]) -> bool:
    name = str(m.get("name", "")).lower()
    url = str(m.get("url", "")).lower()
    for sel in selectors:
        s = sel.strip().lower()
        if s and (s == name or s == url or s in name or s in url):
            return True
    return False


# ----------------------------- Config and profiles -----------------------------
def load_runtime_config(path: str) -> RuntimeOptions:
    opts = RuntimeOptions()
    if not path or not os.path.exists(path):
        return opts
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(path, encoding="utf-8")
    if cp.has_section("connection"):
        opts.url = cp.get("connection", "url", fallback=opts.url).strip()
        opts.username = cp.get("connection", "username", fallback=opts.username).strip()
        opts.password = cp.get("connection", "password", fallback=opts.password).strip()
    if cp.has_section("defaults"):
        opts.default_client = cp.get("defaults", "default_client", fallback=opts.default_client).strip() or opts.default_client
        opts.default_csv = cp.get("defaults", "default_csv", fallback=opts.default_csv).strip() or opts.default_csv
        opts.default_txt = cp.get("defaults", "default_txt", fallback=opts.default_txt).strip() or opts.default_txt
        opts.default_export = cp.get("defaults", "default_export", fallback=opts.default_export).strip() or opts.default_export
        opts.dry_run_default = parse_bool(cp.get("defaults", "dry_run_default", fallback=str(opts.dry_run_default)), opts.dry_run_default)
        opts.verbose_default = parse_bool(cp.get("defaults", "verbose_default", fallback=str(opts.verbose_default)), opts.verbose_default)
        opts.auto_backup_default = parse_bool(cp.get("defaults", "auto_backup_default", fallback=str(opts.auto_backup_default)), opts.auto_backup_default)
    return opts


def load_profiles(path: str) -> Dict[str, Spec]:
    profiles: Dict[str, Spec] = {}
    if not path or not os.path.exists(path):
        return profiles
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(path, encoding="utf-8")
    for section in cp.sections():
        if not section.lower().startswith("profile:"):
            continue
        pname = section.split(":", 1)[1].strip()
        client = cp.get(section, "client", fallback="").strip() or "Clients"
        group = cp.get(section, "group", fallback="").strip() or client
        profiles[pname] = Spec(
            client=client,
            group=group,
            name=cp.get(section, "name", fallback="").strip(),
            url="",
            mtype=cp.get(section, "type", fallback=DEFAULT_TYPE).strip().lower() or DEFAULT_TYPE,
            interval=cp.getint(section, "interval", fallback=DEFAULT_INTERVAL),
            maxretries=cp.getint(section, "maxretries", fallback=DEFAULT_MAX_RETRIES),
            retry_interval=cp.getint(section, "retryInterval", fallback=DEFAULT_RETRY_INTERVAL),
            method=cp.get(section, "method", fallback="GET").strip().upper() or "GET",
            ignore_tls=parse_bool(cp.get(section, "ignore_tls", fallback="false"), False),
            timeout=cp.getint(section, "timeout", fallback=DEFAULT_TIMEOUT),
            accepted_statuscodes=parse_list(cp.get(section, "accepted_statuscodes", fallback="200-299")) or ["200-299"],
            tags=merge_tags(client, group, parse_tags(cp.get(section, "tags", fallback=""))),
            notifications=parse_list(cp.get(section, "notifications", fallback="")),
            status_page=cp.get(section, "status_page", fallback="").strip(),
            public_group=cp.get(section, "public_group", fallback="").strip(),
            keyword=cp.get(section, "keyword", fallback="").strip(),
            expected_value=cp.get(section, "expected_value", fallback="").strip(),
        )
    return profiles


def choose_profile(profiles: Dict[str, Spec]) -> Optional[Spec]:
    if not profiles:
        print("No profiles loaded from defaults file.")
        return None
    names = sorted(profiles.keys())
    print("\nAvailable default profiles")
    print("--------------------------")
    for idx, name in enumerate(names, 1):
        prof = profiles[name]
        print(f"{idx}. {name} (client={prof.client}, group={prof.group}, interval={prof.interval})")
    print("0. Cancel")
    while True:
        sel = input("Select a profile: ").strip()
        if not sel or sel == "0":
            return None
        try:
            idx = int(sel)
            if 1 <= idx <= len(names):
                return profiles[names[idx - 1]]
        except ValueError:
            pass
        print("Invalid selection.")


# ----------------------------- Source loaders and validation -----------------------------
def load_specs_from_csv(csv_path: str) -> List[Spec]:
    specs: List[Spec] = []
    seen: Dict[Tuple[str, str], int] = {}
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or "url" not in reader.fieldnames:
            raise ValueError("CSV must contain at least the 'url' column")
        for idx, row in enumerate(reader, 2):
            url = normalise_url(row.get("url", ""))
            if not url:
                raise ValueError(f"CSV row {idx}: empty URL")
            mtype = (row.get("type") or DEFAULT_TYPE).strip().lower() or DEFAULT_TYPE
            if mtype not in {"http", "https"}:
                raise ValueError(f"CSV row {idx}: unsupported type '{mtype}'")
            client = (row.get("client") or "").strip() or detect_client(url)
            group = (row.get("group") or "").strip() or client
            interval = int(row.get("interval") or DEFAULT_INTERVAL)
            maxr = int(row.get("maxretries") or DEFAULT_MAX_RETRIES)
            retryi = int(row.get("retryInterval") or DEFAULT_RETRY_INTERVAL)
            timeout = int(row.get("timeout") or DEFAULT_TIMEOUT)
            if interval <= 0 or maxr < 0 or retryi < 0 or timeout <= 0:
                raise ValueError(f"CSV row {idx}: interval/retry/timeout values must be positive")
            spec = Spec(
                client=client,
                group=group,
                name=(row.get("name") or "").strip() or (urlparse(url).netloc or url),
                url=url,
                mtype=mtype,
                interval=interval,
                maxretries=maxr,
                retry_interval=retryi,
                tags=merge_tags(client, group, parse_tags(row.get("tags", ""))),
                enabled=parse_bool(row.get("enabled"), True),
                notifications=parse_list(row.get("notifications", "")),
                status_page=(row.get("status_page") or "").strip(),
                public_group=(row.get("public_group") or "").strip(),
                method=(row.get("method") or "GET").strip().upper() or "GET",
                ignore_tls=parse_bool(row.get("ignore_tls"), False),
                timeout=timeout,
                accepted_statuscodes=parse_list(row.get("accepted_statuscodes", "200-299")) or ["200-299"],
                keyword=(row.get("keyword") or "").strip(),
                expected_value=(row.get("expected_value") or "").strip(),
            )
            key = spec_key(spec)
            if key in seen:
                raise ValueError(f"CSV row {idx}: duplicate URL/type '{spec.url}' (first seen at row {seen[key]})")
            seen[key] = idx
            specs.append(spec)
    return specs


def load_specs_from_txt(txt_path: str, default_client: str = "Clients", base_profile: Optional[Spec] = None) -> List[Spec]:
    specs: List[Spec] = []
    seen: set[Tuple[str, str]] = set()
    with open(txt_path, encoding="utf-8") as fh:
        for idx, line in enumerate(fh, 1):
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            url = normalise_url(raw)
            auto_client = detect_client(url) or default_client
            prof = base_profile or Spec(client=auto_client, group=auto_client, name="", url="", mtype="http")
            client = prof.client or auto_client
            group = prof.group or client
            spec = Spec(
                client=client,
                group=group,
                name=prof.name or (urlparse(url).netloc or url),
                url=url,
                mtype=prof.mtype or "http",
                interval=prof.interval or DEFAULT_INTERVAL,
                maxretries=prof.maxretries or DEFAULT_MAX_RETRIES,
                retry_interval=prof.retry_interval or DEFAULT_RETRY_INTERVAL,
                tags=merge_tags(client, group, prof.tags),
                notifications=list(prof.notifications),
                status_page=prof.status_page,
                public_group=prof.public_group,
                method=prof.method or "GET",
                ignore_tls=prof.ignore_tls,
                timeout=prof.timeout or DEFAULT_TIMEOUT,
                accepted_statuscodes=list(prof.accepted_statuscodes or ["200-299"]),
                keyword=prof.keyword,
                expected_value=prof.expected_value,
            )
            key = spec_key(spec)
            if key in seen:
                raise ValueError(f"TXT line {idx}: duplicate URL '{url}'")
            seen.add(key)
            specs.append(spec)
    return specs


def manual_entry_mode(default_client: str = "Clients", base_profile: Optional[Spec] = None) -> List[Spec]:
    print("\nManual Entry Mode")
    print("-----------------")
    print("Enter one domain or URL per prompt. Leave blank to finish.")
    specs: List[Spec] = []
    seen: set[Tuple[str, str]] = set()
    template = base_profile
    while True:
        raw = input("Domain/URL: ").strip()
        if not raw:
            break
        url = normalise_url(raw)
        auto_client = detect_client(url) or default_client
        tmpl = template or Spec(client=auto_client, group=auto_client, name="", url="", mtype="http")
        client = input(f"Client [{tmpl.client or auto_client}]: ").strip() or (tmpl.client or auto_client)
        group = input(f"Group [{tmpl.group or client}]: ").strip() or (tmpl.group or client)
        name = input(f"Name [{tmpl.name or (urlparse(url).netloc or url)}]: ").strip() or (tmpl.name or (urlparse(url).netloc or url))
        spec = Spec(
            client=client,
            group=group,
            name=name,
            url=url,
            mtype=tmpl.mtype or "http",
            interval=int(input(f"Interval seconds [{tmpl.interval or DEFAULT_INTERVAL}]: ").strip() or (tmpl.interval or DEFAULT_INTERVAL)),
            retry_interval=int(input(f"Retry interval seconds [{tmpl.retry_interval or DEFAULT_RETRY_INTERVAL}]: ").strip() or (tmpl.retry_interval or DEFAULT_RETRY_INTERVAL)),
            maxretries=int(input(f"Max retries [{tmpl.maxretries or DEFAULT_MAX_RETRIES}]: ").strip() or (tmpl.maxretries or DEFAULT_MAX_RETRIES)),
            method=(input(f"HTTP method [{tmpl.method or 'GET'}]: ").strip() or (tmpl.method or 'GET')).upper(),
            ignore_tls=yes_no("Ignore TLS errors?", default=tmpl.ignore_tls),
            tags=merge_tags(client, group, parse_tags(input("Extra tags (comma or semicolon separated) [optional]: ").strip()) or tmpl.tags),
            notifications=parse_list(input("Notification names (comma or semicolon separated) [optional]: ").strip()) or list(tmpl.notifications),
            status_page=input(f"Status page name [{tmpl.status_page or 'none'}]: ").strip() or tmpl.status_page,
            public_group=input(f"Public group name [{tmpl.public_group or 'none'}]: ").strip() or tmpl.public_group,
            timeout=tmpl.timeout or DEFAULT_TIMEOUT,
            accepted_statuscodes=tmpl.accepted_statuscodes or ["200-299"],
            keyword=input(f"Keyword to match [{tmpl.keyword or 'none'}]: ").strip() or tmpl.keyword,
            expected_value=input(f"Expected value [{tmpl.expected_value or 'none'}]: ").strip() or tmpl.expected_value,
        )
        key = spec_key(spec)
        if key in seen:
            print(f"WARN: Duplicate URL/type '{spec.url}' skipped", file=sys.stderr)
            continue
        seen.add(key)
        specs.append(spec)
        print(f"Added: {spec.name} -> {spec.url}\n")
    return specs


def validate_source(args, console: Console, opts: RuntimeOptions, profiles: Dict[str, Spec]) -> int:
    try:
        if args.csv:
            specs = load_specs_from_csv(args.csv)
        elif args.txt:
            specs = load_specs_from_txt(args.txt, default_client=opts.default_client, base_profile=profiles.get(args.profile) if args.profile else None)
        else:
            console.error("Validation requires --csv or --txt")
            return 2
        specs = apply_filters(specs, args.client or None, args.limit)
        console.line(f"Validation OK: {len(specs)} monitor definitions parsed successfully", kind="success", force=True)
        return 0
    except Exception as exc:
        console.error(f"Validation failed: {exc}")
        return 2


# ----------------------------- API helpers -----------------------------
def compact_monitor(m: dict) -> dict:
    tag_strings: List[str] = []
    for t in (m.get("tags") or []):
        if isinstance(t, dict):
            name = t.get("name") or t.get("tag") or t.get("tag_name")
            value = t.get("value")
            if name and value not in (None, ""):
                tag_strings.append(f"{name}:{value}")
            elif name:
                tag_strings.append(str(name))
        elif isinstance(t, str):
            tag_strings.append(t)
    notif_names: List[str] = []
    notif_ids: List[int] = []
    for n in (m.get("notifications") or []):
        if isinstance(n, dict):
            if n.get("name"):
                notif_names.append(str(n.get("name")))
            if n.get("id") is not None:
                try:
                    notif_ids.append(int(n.get("id")))
                except Exception:
                    pass
    for nid in (m.get("notificationIDList") or []):
        try:
            notif_ids.append(int(nid))
        except Exception:
            pass
    accepted = m.get("accepted_statuscodes_json") or m.get("acceptedStatusCodes") or m.get("accepted_statuscodes") or []
    if isinstance(accepted, str):
        try:
            accepted = json.loads(accepted)
        except Exception:
            accepted = [accepted]
    return {
        "id": m.get("id"),
        "name": m.get("name"),
        "url": normalise_url(m.get("url") or m.get("hostname") or ""),
        "type": (m.get("type") or "").lower(),
        "interval": int(m.get("interval") or 0),
        "maxretries": int(m.get("maxretries") or 0),
        "retryInterval": int(m.get("retryInterval") or 0),
        "method": (m.get("method") or "GET").upper(),
        "ignoreTls": bool(m.get("ignoreTls", False) or m.get("ignore_tls", False)),
        "timeout": int(m.get("timeout") or 0),
        "accepted_statuscodes": sorted(accepted or []),
        "keyword": m.get("keyword") or "",
        "expected_value": m.get("expectedValue") or m.get("expected_value") or "",
        "tags": sorted(set(tag_strings)),
        "notification_names": sorted(set(notif_names)),
        "notification_ids": sorted(set(notif_ids)),
    }


def get_existing_monitors(api) -> Dict[Tuple[str, str], dict]:
    existing: Dict[Tuple[str, str], dict] = {}
    for raw in api.get_monitors():
        m = compact_monitor(raw)
        key = ((m["type"] or "http"), m["url"])
        if key[1]:
            existing[key] = m
    return existing


def list_existing_monitors(api) -> List[dict]:
    return [compact_monitor(raw) for raw in api.get_monitors()]


def notification_name_to_id(api) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for method_name in ("get_notifications", "get_notification_methods", "get_notification_list"):
        if hasattr(api, method_name):
            try:
                result = getattr(api, method_name)()
                for item in result:
                    if isinstance(item, dict) and item.get("name") and item.get("id") is not None:
                        try:
                            out[str(item["name"])] = int(item["id"])
                        except Exception:
                            pass
                if out:
                    return out
            except Exception:
                continue
    return out


def try_tag_helpers(api, spec: Spec):
    try:
        if hasattr(api, "get_tags") and hasattr(api, "add_tag"):
            existing_tags = {}
            for t in api.get_tags():
                if isinstance(t, dict):
                    nm = t.get("name") or t.get("tag") or t.get("tag_name")
                    if nm:
                        existing_tags[str(nm)] = t
            tag_payload = []
            for text in spec.tags:
                if ":" in text:
                    tag_name, tag_value = text.split(":", 1)
                else:
                    tag_name, tag_value = text, ""
                tag_obj = existing_tags.get(tag_name)
                if not tag_obj:
                    try:
                        created = api.add_tag(tag_name)
                        tag_obj = created if isinstance(created, dict) else {"name": tag_name, "id": created}
                        existing_tags[tag_name] = tag_obj
                    except Exception:
                        tag_obj = {"name": tag_name}
                tag_id = tag_obj.get("id") if isinstance(tag_obj, dict) else None
                if tag_id is not None:
                    tag_payload.append({"tag_id": tag_id, "value": tag_value})
                else:
                    tag_payload.append({"name": tag_name, "value": tag_value})
            return tag_payload if tag_payload else None
    except Exception:
        return None
    return None


def build_monitor_kwargs(spec: Spec, notification_ids: Optional[List[int]], tag_payload=None) -> dict:
    kwargs = {
        "type": spec.mtype,
        "name": spec.name,
        "url": spec.url,
        "interval": spec.interval,
        "maxretries": spec.maxretries,
        "retryInterval": spec.retry_interval,
        "method": spec.method,
        "ignoreTls": spec.ignore_tls,
        "timeout": spec.timeout,
        "accepted_statuscodes": spec.accepted_statuscodes,
    }
    if spec.keyword:
        kwargs["keyword"] = spec.keyword
    if spec.expected_value:
        kwargs["expectedValue"] = spec.expected_value
    if notification_ids:
        kwargs["notificationIDList"] = notification_ids
    if tag_payload is not None:
        kwargs["tags"] = tag_payload
    return kwargs


def desired_repr(spec: Spec, resolved_notification_ids: Optional[List[int]] = None) -> dict:
    return {
        "name": spec.name,
        "url": spec.url,
        "type": spec.mtype,
        "interval": spec.interval,
        "maxretries": spec.maxretries,
        "retryInterval": spec.retry_interval,
        "method": spec.method,
        "ignoreTls": spec.ignore_tls,
        "timeout": spec.timeout,
        "accepted_statuscodes": sorted(spec.accepted_statuscodes),
        "keyword": spec.keyword,
        "expected_value": spec.expected_value,
        "tags": sorted(spec.tags),
        "notification_ids": sorted(resolved_notification_ids or []),
    }


def diff_monitor(existing: dict, desired: dict) -> List[str]:
    changed: List[str] = []
    for field in ["name", "url", "type", "interval", "maxretries", "retryInterval", "method", "ignoreTls", "timeout", "keyword", "expected_value"]:
        if existing.get(field) != desired.get(field):
            changed.append(field)
    if desired.get("accepted_statuscodes") and sorted(existing.get("accepted_statuscodes") or []) != sorted(desired.get("accepted_statuscodes") or []):
        changed.append("accepted_statuscodes")
    if desired.get("tags") and existing.get("tags") and sorted(existing["tags"]) != sorted(desired["tags"]):
        changed.append("tags")
    if desired.get("notification_ids") and sorted(existing.get("notification_ids") or []) != sorted(desired.get("notification_ids") or []):
        changed.append("notifications")
    return changed


def api_call_with_retry(fn, retries: int, delay: float = 1.0):
    attempt = 0
    last_exc = None
    while attempt <= retries:
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt >= retries:
                raise
            time.sleep(delay)
            attempt += 1
    if last_exc:
        raise last_exc


def try_create_or_update(api, current: Optional[dict], spec: Spec, notification_ids: List[int]):
    tag_payload = try_tag_helpers(api, spec)
    kwargs = build_monitor_kwargs(spec, notification_ids, tag_payload)
    if current is None:
        try:
            return api.add_monitor(**kwargs)
        except TypeError:
            kwargs.pop("tags", None)
            return api.add_monitor(**kwargs)
    else:
        try:
            return api.edit_monitor(current["id"], **kwargs)
        except TypeError:
            kwargs.pop("tags", None)
            return api.edit_monitor(current["id"], **kwargs)


def export_monitors(api, out_csv: str) -> None:
    rows = []
    for raw in api.get_monitors():
        m = compact_monitor(raw)
        client = ""
        group = ""
        for tag in m.get("tags", []):
            if isinstance(tag, str) and tag.startswith("Client:"):
                client = tag.split(":", 1)[1]
            elif isinstance(tag, str) and tag.startswith("Group:"):
                group = tag.split(":", 1)[1]
        rows.append({
            "client": client or detect_client(m.get("url", "")),
            "name": m.get("name", ""),
            "url": m.get("url", ""),
            "type": m.get("type", "http"),
            "interval": m.get("interval", DEFAULT_INTERVAL),
            "maxretries": m.get("maxretries", DEFAULT_MAX_RETRIES),
            "retryInterval": m.get("retryInterval", DEFAULT_RETRY_INTERVAL),
            "group": group or client or detect_client(m.get("url", "")),
            "tags": ";".join(m.get("tags", [])),
            "enabled": "true",
            "notifications": ";".join(m.get("notification_names", [])),
            "status_page": "",
            "public_group": "",
            "method": m.get("method", "GET"),
            "ignore_tls": str(m.get("ignoreTls", False)).lower(),
            "timeout": m.get("timeout", DEFAULT_TIMEOUT),
            "accepted_statuscodes": ";".join(m.get("accepted_statuscodes", [])) if isinstance(m.get("accepted_statuscodes"), list) else "200-299",
            "keyword": m.get("keyword", ""),
            "expected_value": m.get("expected_value", ""),
        })
    fieldnames = ["client", "name", "url", "type", "interval", "maxretries", "retryInterval", "group", "tags", "enabled", "notifications", "status_page", "public_group", "method", "ignore_tls", "timeout", "accepted_statuscodes", "keyword", "expected_value"]
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def backup_before_apply(api, backup_dir: str, console: Console) -> str:
    ensure_dir(backup_dir)
    target = os.path.join(backup_dir, f"backup-{timestamp()}.csv")
    export_monitors(api, target)
    console.line(f"Backup exported to {target}", kind="info", force=True)
    return target


def best_effort_attach_to_status_page(api, monitor_id: int, status_page_name: str, public_group_name: str, console: Console) -> None:
    if not status_page_name:
        return
    try:
        pages = api.get_status_pages() if hasattr(api, "get_status_pages") else []
        target_page = None
        for p in pages or []:
            if isinstance(p, dict) and ((p.get("title") == status_page_name) or (p.get("name") == status_page_name) or (p.get("slug") == status_page_name)):
                target_page = p
                break
        if not target_page:
            console.warn(f"WARN: Status page '{status_page_name}' not found; skipping assignment")
            return
        page_id = target_page.get("id")
        for method_name in ("add_public_group_monitor", "add_monitor_to_public_group", "add_status_page_monitor", "add_monitor_to_status_page"):
            if hasattr(api, method_name):
                fn = getattr(api, method_name)
                try:
                    params = list(inspect.signature(fn).parameters)
                    if len(params) >= 3:
                        fn(page_id, public_group_name or "Default", monitor_id)
                    elif len(params) == 2:
                        fn(page_id, monitor_id)
                    else:
                        fn(monitor_id)
                    return
                except Exception:
                    continue
        console.warn(f"WARN: No supported status-page attach method found for '{status_page_name}'")
    except Exception as exc:
        console.warn(f"WARN: Status-page attach failed for '{status_page_name}': {exc}")


# ----------------------------- Delete helpers -----------------------------
def delete_monitors(api, monitors: List[dict], dry_run: bool, retries: int, console: Console) -> Summary:
    summary = Summary()
    total = len(monitors)
    for idx, m in enumerate(monitors, 1):
        label = f"[{m.get('type','http')}] {m.get('name')} -> {m.get('url')}"
        prefix = f"[{idx}/{total}] "
        if dry_run:
            console.line(prefix + f"WOULD DELETE {label}", kind="delete")
            summary.would_delete += 1
            continue
        try:
            api_call_with_retry(lambda: api.delete_monitor(m["id"]), retries=retries)
            console.line(prefix + f"DELETED {label}", kind="delete")
            summary.deleted += 1
        except Exception as exc:
            summary.failed += 1
            console.error(prefix + f"ERROR deleting {label} | {exc}")
    summary.total = total
    return summary


# ----------------------------- Sync / verify core -----------------------------
def print_summary(summary: Summary, console: Console, dry_run: bool, report_unmanaged: bool, delete_missing: bool) -> None:
    changed = summary.created + summary.updated + summary.deleted
    unchanged = summary.skipped
    console.line("\nSummary", kind="info", force=True)
    console.line("-------", kind="info", force=True)
    if dry_run:
        console.line(f"Would create: {summary.would_create}", kind="success" if summary.would_create == 0 else "update", force=True)
        console.line(f"Would update: {summary.would_update}", kind="success" if summary.would_update == 0 else "update", force=True)
        console.line(f"Would skip:   {summary.would_skip}", kind="skip", force=True)
        console.line(f"Would delete: {summary.would_delete}", kind="delete" if summary.would_delete else "success", force=True)
        console.line(f"Would fail:   {summary.failed}", kind="error" if summary.failed else "success", force=True)
    else:
        console.line(f"Created: {summary.created}", kind="success" if summary.created or (summary.created == 0 and summary.failed == 0) else "info", force=True)
        console.line(f"Updated: {summary.updated}", kind="update" if summary.updated else "success", force=True)
        console.line(f"Skipped: {summary.skipped}", kind="skip", force=True)
        console.line(f"Deleted: {summary.deleted}", kind="delete" if summary.deleted else "success", force=True)
        console.line(f"Failed:  {summary.failed}", kind="error" if summary.failed else "success", force=True)
    console.line(f"Total processed: {summary.total}", kind="success" if summary.failed == 0 else "error", force=True)
    console.line(f"Changed: {changed}", kind="update" if changed else "success", force=True)
    console.line(f"No changes: {unchanged}", kind="skip", force=True)
    if report_unmanaged or delete_missing:
        console.line(f"Unmanaged found: {summary.unmanaged}", kind="warn" if summary.unmanaged else "success", force=True)


def run_sync(api, specs: List[Spec], console: Console, dry_run: bool = False, report_unmanaged: bool = False, delete_missing: bool = False, api_retries: int = 2) -> int:
    existing = get_existing_monitors(api)
    notif_map = notification_name_to_id(api)
    summary = Summary()
    desired_keys = {spec_key(s) for s in specs}
    total = len(specs)

    for idx, spec in enumerate(specs, 1):
        current = existing.get(spec_key(spec))
        resolved_notification_ids: List[int] = []
        missing_notifications: List[str] = []
        for name in spec.notifications:
            if name in notif_map:
                resolved_notification_ids.append(notif_map[name])
            else:
                missing_notifications.append(name)
        if missing_notifications:
            console.warn(f"WARN: Notification(s) not found for {spec.name}: {', '.join(missing_notifications)}")
        desired = desired_repr(spec, resolved_notification_ids)
        if current is None:
            action = "create"
            changed_fields = ["new"]
        else:
            changed_fields = diff_monitor(current, desired)
            action = "update" if changed_fields else "skip"

        prefix = f"[{idx}/{total}] "
        label = f"[{spec.client}] {spec.name} -> {spec.url}"
        summary.total += 1
        if action == "skip":
            console.line(prefix + f"SKIP   {label} (already in desired state)", kind="skip")
            summary.skipped += 1
            if dry_run:
                summary.would_skip += 1
            continue

        console.line(prefix + f"{action.upper():6} {label} | changes: {', '.join(changed_fields)}", kind="create" if action == "create" else "update")
        if dry_run:
            if action == "create":
                summary.would_create += 1
            else:
                summary.would_update += 1
            continue

        try:
            result = api_call_with_retry(lambda: try_create_or_update(api, current, spec, resolved_notification_ids), retries=api_retries)
            monitor_id = result.get("monitorID") if isinstance(result, dict) else None
            if isinstance(result, dict) and not monitor_id:
                monitor_id = result.get("id") or result.get("monitorId")
            if action == "create":
                summary.created += 1
            else:
                summary.updated += 1
            if monitor_id and (spec.status_page or spec.public_group):
                best_effort_attach_to_status_page(api, int(monitor_id), spec.status_page, spec.public_group, console)
        except Exception as exc:
            summary.failed += 1
            console.error(prefix + f"ERROR  {label} | {exc}")

    unmanaged = [m for k, m in existing.items() if k not in desired_keys]
    if report_unmanaged or delete_missing:
        for m in unmanaged:
            console.line(f"UNMANAGED [{m.get('type','http')}] {m.get('name')} -> {m.get('url')}", kind="warn")
            summary.unmanaged += 1
        if delete_missing and unmanaged:
            sub = delete_monitors(api, unmanaged, dry_run=dry_run, retries=api_retries, console=console)
            summary.deleted += sub.deleted
            summary.failed += sub.failed
            summary.would_delete += sub.would_delete

    print_summary(summary, console, dry_run, report_unmanaged, delete_missing)
    return 1 if summary.failed else 0


def export_and_verify(api, export_path: str, console: Console, api_retries: int, client: Optional[str] = None, limit: Optional[int] = None) -> int:
    export_monitors(api, export_path)
    console.line(f"Exported monitors to {export_path}", kind="info", force=True)
    specs = load_specs_from_csv(export_path)
    specs = apply_filters(specs, client, limit)
    return run_sync(api, specs, console, dry_run=True, report_unmanaged=False, delete_missing=False, api_retries=api_retries)


# ----------------------------- Connectivity and interactive UI -----------------------------
def connect_api(opts: RuntimeOptions, cli_url: Optional[str], cli_user: Optional[str], cli_password: Optional[str]):
    base_url = cli_url or opts.url or prompt_env("KUMA_URL", "Enter Uptime Kuma URL: ")
    user = cli_user or opts.username or prompt_env("KUMA_USERNAME", "Enter Uptime Kuma username: ")
    pwd = cli_password or opts.password or prompt_env("KUMA_PASSWORD", "Enter Uptime Kuma password: ", secret=True)
    api = UptimeKumaApi(base_url)
    api.login(user, pwd)
    return api


def interactive_menu(api, opts: RuntimeOptions, profiles: Dict[str, Spec], console: Console, api_retries: int) -> int:
    while True:
        print("\nkuma-importer")
        print("-------------")
        print("1. Import from CSV")
        print("2. Import from TXT")
        print("3. Manual entry mode")
        print("4. Manual entry mode using a defaults profile (clone defaults)")
        print("5. Export existing monitors to CSV")
        print("6. Export and verify current monitors")
        print("7. Report unmanaged monitors")
        print("8. Delete unmanaged monitors")
        print("9. Delete selected monitors")
        print("10. Delete all monitors")
        print("11. Show loaded defaults profiles")
        print("12. Quit")
        choice = input("Select an option [1-12]: ").strip()
        try:
            if choice == "1":
                path = input(f"CSV path [{opts.default_csv}]: ").strip() or opts.default_csv
                specs = load_specs_from_csv(path)
                dry = yes_no("Dry-run only?", default=opts.dry_run_default)
                if not dry and opts.auto_backup_default and yes_no("Export backup before applying changes?", default=True):
                    backup_before_apply(api, DEFAULT_BACKUP_DIR, console)
                run_sync(api, specs, console, dry_run=dry, api_retries=api_retries)
            elif choice == "2":
                path = input(f"TXT path [{opts.default_txt}]: ").strip() or opts.default_txt
                base_profile = choose_profile(profiles) if yes_no("Apply a defaults profile to all TXT entries?", default=False) else None
                specs = load_specs_from_txt(path, default_client=opts.default_client, base_profile=base_profile)
                dry = yes_no("Dry-run only?", default=opts.dry_run_default)
                if not dry and opts.auto_backup_default and yes_no("Export backup before applying changes?", default=True):
                    backup_before_apply(api, DEFAULT_BACKUP_DIR, console)
                run_sync(api, specs, console, dry_run=dry, api_retries=api_retries)
            elif choice == "3":
                specs = manual_entry_mode(default_client=opts.default_client)
                if not specs:
                    print("No entries captured.")
                    continue
                dry = yes_no("Dry-run before applying?", default=opts.dry_run_default)
                rc = run_sync(api, specs, console, dry_run=dry, api_retries=api_retries)
                if dry and rc == 0 and yes_no("Apply these manual entries now?", default=False):
                    if opts.auto_backup_default and yes_no("Export backup before applying changes?", default=True):
                        backup_before_apply(api, DEFAULT_BACKUP_DIR, console)
                    run_sync(api, specs, console, dry_run=False, api_retries=api_retries)
            elif choice == "4":
                profile = choose_profile(profiles)
                if not profile:
                    continue
                specs = manual_entry_mode(default_client=opts.default_client, base_profile=profile)
                if not specs:
                    print("No entries captured.")
                    continue
                dry = yes_no("Dry-run before applying?", default=opts.dry_run_default)
                rc = run_sync(api, specs, console, dry_run=dry, api_retries=api_retries)
                if dry and rc == 0 and yes_no("Apply these manual entries now?", default=False):
                    if opts.auto_backup_default and yes_no("Export backup before applying changes?", default=True):
                        backup_before_apply(api, DEFAULT_BACKUP_DIR, console)
                    run_sync(api, specs, console, dry_run=False, api_retries=api_retries)
            elif choice == "5":
                out_csv = input(f"Export CSV path [{opts.default_export}]: ").strip() or opts.default_export
                export_monitors(api, out_csv)
                console.line(f"Exported monitors to {out_csv}", kind="info", force=True)
            elif choice == "6":
                out_csv = input(f"Export CSV path [{opts.default_export}]: ").strip() or opts.default_export
                export_and_verify(api, out_csv, console, api_retries=api_retries)
            elif choice == "7":
                mode = input("Compare against (c)sv, (t)xt, (m)anual, or (p)rofile+manual? [c/t/m/p]: ").strip().lower() or "c"
                if mode == "c":
                    path = input(f"CSV path [{opts.default_csv}]: ").strip() or opts.default_csv
                    specs = load_specs_from_csv(path)
                elif mode == "t":
                    path = input(f"TXT path [{opts.default_txt}]: ").strip() or opts.default_txt
                    profile = choose_profile(profiles) if yes_no("Apply a defaults profile to TXT entries?", default=False) else None
                    specs = load_specs_from_txt(path, default_client=opts.default_client, base_profile=profile)
                elif mode == "p":
                    profile = choose_profile(profiles)
                    if not profile:
                        continue
                    specs = manual_entry_mode(default_client=opts.default_client, base_profile=profile)
                else:
                    specs = manual_entry_mode(default_client=opts.default_client)
                run_sync(api, specs, console, dry_run=True, report_unmanaged=True, api_retries=api_retries)
            elif choice == "8":
                mode = input("Delete relative to (c)sv, (t)xt, (m)anual, or (p)rofile+manual source? [c/t/m/p]: ").strip().lower() or "c"
                if mode == "c":
                    path = input(f"CSV path [{opts.default_csv}]: ").strip() or opts.default_csv
                    specs = load_specs_from_csv(path)
                elif mode == "t":
                    path = input(f"TXT path [{opts.default_txt}]: ").strip() or opts.default_txt
                    profile = choose_profile(profiles) if yes_no("Apply a defaults profile to TXT entries?", default=False) else None
                    specs = load_specs_from_txt(path, default_client=opts.default_client, base_profile=profile)
                elif mode == "p":
                    profile = choose_profile(profiles)
                    if not profile:
                        continue
                    specs = manual_entry_mode(default_client=opts.default_client, base_profile=profile)
                else:
                    specs = manual_entry_mode(default_client=opts.default_client)
                if not specs:
                    print("No source entries captured; aborting delete operation.")
                    continue
                print("First running dry-run delete report...")
                run_sync(api, specs, console, dry_run=True, report_unmanaged=True, delete_missing=True, api_retries=api_retries)
                if yes_no("Proceed with deleting unmanaged monitors?", default=False):
                    if input('Type DELETE to confirm: ').strip() != 'DELETE':
                        print('Confirmation text did not match. Aborting.')
                        continue
                    if opts.auto_backup_default and yes_no("Export backup before deleting?", default=True):
                        backup_before_apply(api, DEFAULT_BACKUP_DIR, console)
                    run_sync(api, specs, console, dry_run=False, report_unmanaged=True, delete_missing=True, api_retries=api_retries)
            elif choice == "9":
                selectors = input("Enter monitor names or URLs (comma-separated): ").strip()
                if not selectors:
                    continue
                targets = [m for m in list_existing_monitors(api) if selected_match(m, parse_list(selectors))]
                if not targets:
                    print("No matching monitors found.")
                    continue
                delete_monitors(api, targets, dry_run=True, retries=api_retries, console=console)
                if yes_no("Proceed with deleting selected monitors?", default=False):
                    if input('Type DELETE to confirm: ').strip() != 'DELETE':
                        print('Confirmation text did not match. Aborting.')
                        continue
                    if opts.auto_backup_default and yes_no("Export backup before deleting?", default=True):
                        backup_before_apply(api, DEFAULT_BACKUP_DIR, console)
                    summary = delete_monitors(api, targets, dry_run=False, retries=api_retries, console=console)
                    print_summary(summary, console, dry_run=False, report_unmanaged=False, delete_missing=False)
            elif choice == "10":
                targets = list_existing_monitors(api)
                if not targets:
                    print("No monitors found.")
                    continue
                delete_monitors(api, targets, dry_run=True, retries=api_retries, console=console)
                if yes_no("Proceed with deleting ALL monitors?", default=False):
                    if input('Type DELETE ALL to confirm: ').strip() != 'DELETE ALL':
                        print('Confirmation text did not match. Aborting.')
                        continue
                    if opts.auto_backup_default and yes_no("Export backup before deleting all monitors?", default=True):
                        backup_before_apply(api, DEFAULT_BACKUP_DIR, console)
                    summary = delete_monitors(api, targets, dry_run=False, retries=api_retries, console=console)
                    print_summary(summary, console, dry_run=False, report_unmanaged=False, delete_missing=False)
            elif choice == "11":
                if not profiles:
                    print("No defaults profiles loaded.")
                else:
                    print("\nLoaded profiles")
                    print("---------------")
                    for name, prof in sorted(profiles.items()):
                        print(f"- {name}: client={prof.client}, group={prof.group}, interval={prof.interval}, retries={prof.maxretries}, tags={';'.join(prof.tags)}")
            elif choice == "12":
                return 0
            else:
                print("Invalid choice. Please enter a number from 1 to 12.")
        except Exception as exc:
            console.error(f"ERROR: {exc}")


# ----------------------------- Main entry point -----------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CSV/TXT/Interactive Uptime Kuma monitor importer")
    src = parser.add_mutually_exclusive_group(required=False)
    src.add_argument("--csv", help="Path to CSV input file")
    src.add_argument("--txt", help="Path to TXT input file (one URL/domain per line)")
    parser.add_argument("--interactive", action="store_true", help="Start the interactive shell menu")
    parser.add_argument("--manual", action="store_true", help="Start manual entry mode directly")
    parser.add_argument("--export", help="Export existing monitors to the given CSV path and exit")
    parser.add_argument("--export-verify", help="Export existing monitors to the given CSV path and immediately verify the exported state")
    parser.add_argument("--validate", action="store_true", help="Validate the provided CSV/TXT source and exit")
    parser.add_argument("--verify", action="store_true", help="Alias for a quiet audit/dry-run")
    parser.add_argument("--audit", action="store_true", help="Alias for a quiet audit/dry-run")
    parser.add_argument("--url", default=os.getenv("KUMA_URL"), help="Base URL of Uptime Kuma (or set KUMA_URL)")
    parser.add_argument("--username", default=os.getenv("KUMA_USERNAME"), help="Kuma username (or set KUMA_USERNAME)")
    parser.add_argument("--password", default=os.getenv("KUMA_PASSWORD"), help="Kuma password (or set KUMA_PASSWORD)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without applying it")
    parser.add_argument("--verbose", action="store_true", help="Extra output for troubleshooting")
    parser.add_argument("--quiet", action="store_true", help="Reduce output to summary only")
    parser.add_argument("--no-colour", action="store_true", help="Disable coloured output")
    parser.add_argument("--log-file", default="", help="Write all output lines to the specified log file")
    parser.add_argument("--default-client", default=None, help="Fallback client for TXT/manual imports")
    parser.add_argument("--report-unmanaged", action="store_true", help="Report existing monitors missing from the desired source")
    parser.add_argument("--delete-missing", action="store_true", help="Delete existing monitors missing from the desired source")
    parser.add_argument("--delete-selected", default="", help="Delete selected monitors by comma-separated name/URL match")
    parser.add_argument("--delete-all", action="store_true", help="Delete all monitors")
    parser.add_argument("--confirm", default="", help="Confirmation text for destructive operations (DELETE or DELETE ALL)")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help=f"Path to runtime config file (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--profiles", default=DEFAULT_PROFILES_PATH, help=f"Path to defaults/profile config file (default: {DEFAULT_PROFILES_PATH})")
    parser.add_argument("--profile", default="", help="Optional defaults profile name to apply to TXT/manual usage")
    parser.add_argument("--client", default="", help="Only process a specific client from the source file")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N matching records")
    parser.add_argument("--backup-before-apply", action="store_true", help="Export a backup before applying non-dry-run changes")
    parser.add_argument("--no-backup-before-apply", action="store_true", help="Do not export a backup before applying changes")
    parser.add_argument("--backup-dir", default=DEFAULT_BACKUP_DIR, help=f"Directory for automatic backups (default: {DEFAULT_BACKUP_DIR})")
    parser.add_argument("--api-retries", type=int, default=2, help="Retry failed API operations this many times")
    args = parser.parse_args(argv)

    opts = load_runtime_config(args.config)
    if args.default_client:
        opts.default_client = args.default_client
    profiles = load_profiles(args.profiles)
    chosen_profile = profiles.get(args.profile) if args.profile else None
    console = Console(quiet=args.quiet, colour=not args.no_colour, log_file=args.log_file)

    try:
        if args.verify or args.audit:
            args.dry_run = True
        if args.verify or args.audit:
            console.quiet = True

        if not args.interactive and not args.csv and not args.txt and not args.export and not args.export_verify and not args.manual and not args.delete_all and not args.delete_selected:
            args.interactive = True

        if args.validate:
            return validate_source(args, console, opts, profiles)

        api = connect_api(opts, args.url, args.username, args.password)

        if args.export:
            export_monitors(api, args.export)
            console.line(f"Exported monitors to {args.export}", kind="info", force=True)
            return 0

        if args.export_verify:
            return export_and_verify(api, args.export_verify, console, api_retries=args.api_retries, client=args.client or None, limit=args.limit)

        want_backup = args.backup_before_apply or (opts.auto_backup_default and not args.no_backup_before_apply)

        # Destructive CLI operations first
        if args.delete_all or args.delete_selected:
            targets = list_existing_monitors(api)
            if args.delete_selected:
                selectors = parse_list(args.delete_selected)
                targets = [m for m in targets if selected_match(m, selectors)]
            if args.client:
                targets = [m for m in targets if any(t == f"Client:{args.client}" for t in m.get("tags", []))]
            if args.limit is not None and args.limit >= 0:
                targets = targets[:args.limit]
            if not targets:
                console.line("No matching monitors found.", kind="info", force=True)
                return 0
            if args.dry_run:
                summary = delete_monitors(api, targets, dry_run=True, retries=args.api_retries, console=console)
                print_summary(summary, console, dry_run=True, report_unmanaged=False, delete_missing=False)
                return 0
            expected = "DELETE ALL" if args.delete_all else "DELETE"
            if args.confirm != expected:
                console.error(f"Destructive operation requires --confirm \"{expected}\"")
                return 2
            if want_backup:
                backup_before_apply(api, args.backup_dir, console)
            summary = delete_monitors(api, targets, dry_run=False, retries=args.api_retries, console=console)
            print_summary(summary, console, dry_run=False, report_unmanaged=False, delete_missing=False)
            return 1 if summary.failed else 0

        if args.interactive:
            return interactive_menu(api, opts, profiles, console, api_retries=args.api_retries)

        if args.manual:
            specs = manual_entry_mode(default_client=opts.default_client, base_profile=chosen_profile)
        elif args.csv:
            specs = load_specs_from_csv(args.csv)
        else:
            specs = load_specs_from_txt(args.txt, default_client=opts.default_client, base_profile=chosen_profile)

        specs = apply_filters(specs, args.client or None, args.limit)
        if not specs:
            console.line("No valid monitors found in source. Nothing to do.", kind="info", force=True)
            return 0

        if not args.dry_run and want_backup:
            backup_before_apply(api, args.backup_dir, console)

        return run_sync(api, specs, console, dry_run=args.dry_run, report_unmanaged=args.report_unmanaged, delete_missing=args.delete_missing, api_retries=args.api_retries)
    except Exception as exc:
        console.error(f"ERROR: {exc}")
        return 2
    finally:
        console.close()


if __name__ == "__main__":
    raise SystemExit(main())
