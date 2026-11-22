"""Opt-in call playbooks on top of phone_prepare: new-patient checks (#76) and hold waits (#91).

Nothing here dials. Each helper turns a request into ``phone_prepare`` arguments; every call
still gets its own approval card and the user's own "call" reply. State lives in one
append-only ``<slug>.jsonl`` per playbook under the workspace's Hermes home, so the doctor
list and answers (health information) never leave the workspace.

Both playbooks are off until the user opts in (an ``opt_in`` record with their own words).

CLI (``python -m hermes_phone.playbooks``; prints JSON; the skill lists every flag):
    status|opt-out|dry-run <slug>      opt-in <slug> --said "<user's words>"
    plan new-patient-check --list doctors.json --plan "<plan>" --on-behalf-of "<name>"
    plan hold-wait --number +1... --callee "<org>" --reach "<dept>" --reason "<why>" \
        --on-behalf-of "<name>"
    log <slug> --entry-id <id> --call-id <cid> --answer <answer> [--plan-accepted ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import safety

NEW_PATIENT = "new-patient-check"
HOLD_WAIT = "hold-wait"
SLUGS = (NEW_PATIENT, HOLD_WAIT)

NEW_PATIENT_ANSWERS = ("yes", "no", "waitlist", "unknown")
PLAN_ANSWERS = ("yes", "no", "unknown")
HOLD_ANSWERS = ("human_reached", "gave_up", "menu_failed")
NEW_PATIENT_MAX_S = 300
HOLD_MAX_S = 900  # the brief's hard cap (brief.HARD_MAX_DURATION_S); no new cap is introduced
MAX_BATCH = 20

# Spoken by the voice agent; nothing in either brief commits the owner to anything.
NO_COMMITMENT_RULE = (
    "Only ask and listen. Do not book, schedule, register, agree to any charge, give consent, "
    "or accept any terms for the user. If asked to commit to anything, say the user will "
    "decide and call back.")


class PlaybookError(ValueError):
    pass


def state_dir(environ: dict | None = None) -> Path:
    env = os.environ if environ is None else environ
    home = env.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home) / "phone-playbooks"


def state_path(slug: str, environ: dict | None = None) -> Path:
    _check_slug(slug)
    return state_dir(environ) / f"{slug}.jsonl"


def _check_slug(slug: str) -> None:
    if slug not in SLUGS:
        raise PlaybookError(f"unknown playbook {slug!r}; use one of {', '.join(SLUGS)}")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def append(slug: str, record: dict, environ: dict | None = None) -> dict:
    path = state_path(slug, environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    row = {"ts": _now(), **record}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    return row


def read(slug: str, environ: dict | None = None) -> list[dict]:
    path = state_path(slug, environ)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def opted_in(slug: str, environ: dict | None = None) -> bool:
    state = False
    for row in read(slug, environ):
        if row.get("kind") == "opt_in":
            state = True
        elif row.get("kind") == "opt_out":
            state = False
    return state


def opt_in(slug: str, said: str, environ: dict | None = None) -> dict:
    said = (said or "").strip()
    if not said:
        raise PlaybookError("opt-in needs the user's own words (--said)")
    return append(slug, {"kind": "opt_in", "said": said[:300]}, environ)


def opt_out(slug: str, environ: dict | None = None) -> dict:
    return append(slug, {"kind": "opt_out"}, environ)


def _require_opt_in(slug: str, environ: dict | None) -> None:
    if not opted_in(slug, environ):
        raise PlaybookError(
            f"{slug} is off. It is opt-in: ask the user whether to turn it on, and record their "
            f"answer with `opt-in {slug} --said \"...\"` before planning calls.")


def _entry_id(*parts: str) -> str:
    return "e" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:8]


def _number(raw: Any) -> str:
    e164, reason = safety.validate_number(str(raw or ""))
    if e164 is None:
        raise PlaybookError(reason)
    return e164


def _name(on_behalf_of: str) -> str:
    name = (on_behalf_of or "").strip()
    if not name or name.lower() in {"my user", "the user", "user"}:
        raise PlaybookError("on_behalf_of must be the user's real name")
    return name


# ---- #76 new-patient check --------------------------------------------------------------------
def new_patient_args(doctor: dict, plan: str, on_behalf_of: str) -> dict:
    """phone_prepare arguments for one office. Shares only the plan name and the user's name."""
    name = str(doctor.get("name") or "").strip()
    if not name:
        raise PlaybookError("each doctor needs a name")
    plan = (plan or "").strip()
    if not plan:
        raise PlaybookError("the insurance plan name is required")
    office = str(doctor.get("office") or "").strip()
    callee = f"{name}'s office" + (f" ({office})" if office else "")
    facts = {
        "person_to_see": name,
        "insurance_plan": plan,
        "questions": ("1) Is this doctor accepting new patients? 2) Do they take the "
                      f"{plan} plan? 3) If not accepting, is there a waitlist or another "
                      "doctor in the practice who is?"),
        "rules": NO_COMMITMENT_RULE + " Do not share any health condition or reason for the visit.",
    }
    specialty = str(doctor.get("specialty") or "").strip()
    if specialty:
        facts["specialty"] = specialty
    return {
        "callee_name": callee,
        "number": _number(doctor.get("number")),
        "number_source": str(doctor.get("number_source") or "user"),
        "on_behalf_of": _name(on_behalf_of),
        "goal": (f"Ask {name}'s office whether {name} is accepting new patients and whether "
                 f"they take the {plan} insurance plan; do not book anything."),
        "facts_to_share": facts,
        "max_duration_s": NEW_PATIENT_MAX_S,
        "record": False,
        "leave_voicemail": False,
        "inquiry_only": True,
    }


def plan_new_patient(doctors: Iterable[dict], plan: str, on_behalf_of: str,
                     environ: dict | None = None) -> dict:
    _require_opt_in(NEW_PATIENT, environ)
    doctors = list(doctors)
    if not doctors:
        raise PlaybookError("the in-network list is empty")
    if len(doctors) > MAX_BATCH:
        raise PlaybookError(f"at most {MAX_BATCH} offices per list; split it")
    latest = {r.get("entry_id"): r.get("answer") for r in read(NEW_PATIENT, environ)
              if r.get("kind") == "answer"}  # the last answer wins, as in summary()
    done = {eid for eid, ans in latest.items() if ans in ("yes", "no", "waitlist")}
    entries, skipped = [], []
    for doc in doctors:
        args = new_patient_args(doc, plan, on_behalf_of)
        eid = _entry_id(NEW_PATIENT, args["number"], str(doc.get("name")), plan)
        if eid in done:
            skipped.append({"entry_id": eid, "name": doc.get("name"), "why": "already answered"})
            continue
        entries.append({"entry_id": eid, "name": doc.get("name"), "number": args["number"],
                        "phone_prepare": args})
    append(NEW_PATIENT, {"kind": "plan", "plan": plan,
                         "entries": [{"entry_id": e["entry_id"], "name": e["name"],
                                      "number": e["number"]} for e in entries]}, environ)
    return {"playbook": NEW_PATIENT, "entries": entries, "skipped": skipped,
            "approval": ("Show the user this full list. Then prepare ONE call at a time with "
                         "phone_prepare; each call has its own approval card and dials only on "
                         "the user's own \"call\" reply. Never call phone_call yourself and never "
                         "prepare the next office until the previous call has ended.")}


# ---- #91 hold wait ----------------------------------------------------------------------------
def hold_wait_args(number: str, callee: str, reach: str, reason: str, on_behalf_of: str,
                   menu_hints: str = "") -> dict:
    callee = (callee or "").strip()
    reach = (reach or "").strip()
    reason = (reason or "").strip()
    if not callee or not reach or not reason:
        raise PlaybookError("callee, reach (department or person) and reason are required")
    name = _name(on_behalf_of)
    facts = {
        "department_to_reach": reach,
        "reason_for_calling": reason,
        "on_reaching_a_person": (
            f"Say you are an AI assistant calling for {name}, who wants to speak with them about "
            f"{reason}. Ask for their name, a direct number or extension, and any reference or "
            f"ticket number, and the best time for {name} to call back. Then thank them and end "
            "the call so the user is alerted right away."),
        "rules": NO_COMMITMENT_RULE + " Do not answer account, identity or security questions.",
    }
    if menu_hints.strip():
        facts["menu_hints"] = menu_hints.strip()[:300]
    return {
        "callee_name": callee,
        "number": _number(number),
        "number_source": "user",
        "on_behalf_of": name,
        "goal": (f"Get through {callee}'s phone menu to {reach}, wait on hold, and when a person "
                 f"answers, get their name and a direct line for {name}; agree to nothing."),
        "facts_to_share": facts,
        "max_duration_s": HOLD_MAX_S,
        "record": False,
        "leave_voicemail": False,
        "inquiry_only": True,
    }


def plan_hold_wait(number: str, callee: str, reach: str, reason: str, on_behalf_of: str,
                   menu_hints: str = "", environ: dict | None = None) -> dict:
    _require_opt_in(HOLD_WAIT, environ)
    args = hold_wait_args(number, callee, reach, reason, on_behalf_of, menu_hints)
    eid = _entry_id(HOLD_WAIT, args["number"], reach, _now())
    append(HOLD_WAIT, {"kind": "plan", "entry_id": eid, "callee": callee,
                       "number": args["number"], "reach": reach}, environ)
    return {"playbook": HOLD_WAIT, "entries": [{"entry_id": eid, "phone_prepare": args}],
            "approval": ("Prepare this one call with phone_prepare; it dials only on the user's "
                         "own \"call\" reply. Holds longer than 15 minutes end the call; offer a "
                         "new approved call rather than redialing.")}


# ---- answers ----------------------------------------------------------------------------------
def log_answer(slug: str, entry_id: str, call_id: str, answer: str, plan_accepted: str = "unknown",
               note: str = "", environ: dict | None = None) -> dict:
    _check_slug(slug)
    allowed = NEW_PATIENT_ANSWERS if slug == NEW_PATIENT else HOLD_ANSWERS
    if answer not in allowed:
        raise PlaybookError(f"answer must be one of {', '.join(allowed)}")
    if not entry_id or not call_id:
        raise PlaybookError("entry_id and call_id are required: log only answers from a real "
                            "call result (phone_status), never a guess")
    row = {"kind": "answer", "entry_id": entry_id, "call_id": call_id, "answer": answer,
           "note": (note or "")[:500]}
    if slug == NEW_PATIENT:
        if plan_accepted not in PLAN_ANSWERS:
            raise PlaybookError(f"plan_accepted must be one of {', '.join(PLAN_ANSWERS)}")
        row["plan_accepted"] = plan_accepted
    return append(slug, row, environ)


def summary(slug: str, environ: dict | None = None) -> dict:
    rows = read(slug, environ)
    answers: dict[str, dict] = {}
    names: dict[str, str] = {}
    for r in rows:
        if r.get("kind") == "plan":
            for e in r.get("entries") or [r]:
                if e.get("entry_id"):
                    names[e["entry_id"]] = e.get("name") or e.get("callee") or ""
        elif r.get("kind") == "answer":
            answers[r["entry_id"]] = r
    return {"playbook": slug, "opted_in": opted_in(slug, environ),
            "answers": [{"entry_id": k, "name": names.get(k, ""), **{
                f: v[f] for f in ("answer", "plan_accepted", "call_id", "note", "ts") if f in v}}
                for k, v in answers.items()],
            "pending": [{"entry_id": k, "name": n} for k, n in names.items() if k not in answers]}


# ---- dry run: synthetic fixtures, simulated call, never dials ---------------------------------
SYNTHETIC_DOCTORS = [  # 555-01xx numbers are reserved for fiction
    {"name": "Dr. Ada Example", "office": "Sample Family Medicine", "specialty": "primary care",
     "number": "+12125550101"},
    {"name": "Dr. Ben Fixture", "office": "Placeholder Health", "specialty": "dermatology",
     "number": "+12125550102"},
]
SYNTHETIC_RESULTS = [("yes", "yes", "Accepting new patients; takes the plan."),
                     ("waitlist", "yes", "Six-week waitlist; takes the plan.")]


def dry_run(slug: str, environ: dict) -> dict:
    """Run a playbook end to end against synthetic data in a throwaway home. No dial path is
    touched: the 'call' is a canned result written as if phone_status had returned it."""
    _check_slug(slug)
    opt_in(slug, "dry run: synthetic fixtures", environ)
    if slug == NEW_PATIENT:
        planned = plan_new_patient(SYNTHETIC_DOCTORS, "Example PPO Gold", "Test Owner", environ)
        for i, (entry, (ans, plan_ok, note)) in enumerate(zip(planned["entries"],
                                                              SYNTHETIC_RESULTS, strict=True)):
            log_answer(slug, entry["entry_id"], f"csim{i:04d}", ans, plan_ok, note, environ)
        again = plan_new_patient(SYNTHETIC_DOCTORS, "Example PPO Gold", "Test Owner", environ)
    else:
        planned = plan_hold_wait("+18005550199", "Example Insurance Co", "claims",
                                 "a denied claim", "Test Owner", "press 2 for claims", environ)
        entry = planned["entries"][0]
        log_answer(slug, entry["entry_id"], "csim0000", "human_reached",
                   note="Simulated: menu 2, 11 min hold, agent Sam, ext 4410, ref ABC123.",
                   environ=environ)
        again = None
    return {"playbook": slug, "dialed": False, "planned": planned,
            "replanned_skips": again["skipped"] if again else None,
            "summary": summary(slug, environ)}


# ---- CLI --------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m hermes_phone.playbooks")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("status", "opt-out", "dry-run"):
        sub.add_parser(name).add_argument("slug")
    o = sub.add_parser("opt-in")
    o.add_argument("slug")
    o.add_argument("--said", required=True)
    pl = sub.add_parser("plan")
    pl.add_argument("slug")
    pl.add_argument("--list", dest="list_path")
    pl.add_argument("--plan", default="")
    pl.add_argument("--on-behalf-of", required=True)
    pl.add_argument("--number", default="")
    pl.add_argument("--callee", default="")
    pl.add_argument("--reach", default="")
    pl.add_argument("--reason", default="")
    pl.add_argument("--menu-hints", default="")
    lg = sub.add_parser("log")
    lg.add_argument("slug")
    lg.add_argument("--entry-id", required=True)
    lg.add_argument("--call-id", required=True)
    lg.add_argument("--answer", required=True)
    lg.add_argument("--plan-accepted", default="unknown")
    lg.add_argument("--note", default="")
    a = p.parse_args(argv)
    try:
        if a.cmd == "status":
            out: Any = summary(a.slug)
        elif a.cmd == "opt-in":
            out = opt_in(a.slug, a.said)
        elif a.cmd == "opt-out":
            out = opt_out(a.slug)
        elif a.cmd == "dry-run":
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                out = dry_run(a.slug, {"HERMES_HOME": tmp})
        elif a.cmd == "plan":
            if a.slug == NEW_PATIENT:
                if not a.list_path:
                    raise PlaybookError("--list <doctors.json> is required")
                doctors = json.loads(Path(a.list_path).read_text(encoding="utf-8"))
                out = plan_new_patient(doctors, a.plan, a.on_behalf_of)
            else:
                _check_slug(a.slug)
                out = plan_hold_wait(a.number, a.callee, a.reach, a.reason, a.on_behalf_of,
                                     a.menu_hints)
        else:
            out = log_answer(a.slug, a.entry_id, a.call_id, a.answer, a.plan_accepted, a.note)
    except (PlaybookError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
