#!/usr/bin/env python3
"""
Claude Code usage breakdown by skill / subagent / model.

Two modes:
  1. CLI report (default): prints text breakdown to stdout
  2. JSON export (--json): writes structured data for the dashboard

Usage:
  ./workspace/bin/ws python workspace/scripts/usage_breakdown.py [--days N] [--date YYYY-MM-DD]
  ./workspace/bin/ws python workspace/scripts/usage_breakdown.py --json --out workspace/usage-dashboard/data.json --days 7
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
from collections import defaultdict

# Per-million-token prices (USD). https://www.anthropic.com/pricing#api
PRICES = {
    "claude-opus-4-7":     {"in": 15.00, "out": 75.00, "cache_read": 1.50,  "cache_write_5m": 18.75, "cache_write_1h": 30.00},
    "claude-sonnet-4-6":   {"in": 3.00,  "out": 15.00, "cache_read": 0.30,  "cache_write_5m": 3.75,  "cache_write_1h": 6.00},
    "claude-haiku-4-5":    {"in": 1.00,  "out": 5.00,  "cache_read": 0.10,  "cache_write_5m": 1.25,  "cache_write_1h": 2.00},
}

def model_key(model: str) -> str:
    if not model: return "unknown"
    m = model.lower()
    if "opus" in m: return "claude-opus-4-7"
    if "sonnet" in m: return "claude-sonnet-4-6"
    if "haiku" in m: return "claude-haiku-4-5"
    return model

def cost_for(usage: dict, model: str) -> float:
    p = PRICES.get(model_key(model))
    if not p: return 0.0
    inp = usage.get("input_tokens", 0) or 0
    out = usage.get("output_tokens", 0) or 0
    cr  = usage.get("cache_read_input_tokens", 0) or 0
    cw  = usage.get("cache_creation_input_tokens", 0) or 0
    cw5 = (usage.get("cache_creation") or {}).get("ephemeral_5m_input_tokens", 0) or 0
    cw1 = (usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens", 0) or 0
    if cw5 or cw1:
        cw_cost = cw5 * p["cache_write_5m"] / 1e6 + cw1 * p["cache_write_1h"] / 1e6
    else:
        cw_cost = cw * p["cache_write_5m"] / 1e6
    return (inp * p["in"] + out * p["out"] + cr * p["cache_read"]) / 1e6 + cw_cost

def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--date", help="Specific date YYYY-MM-DD (overrides --days)")
    ap.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--json", action="store_true", help="Emit JSON for dashboard")
    ap.add_argument("--out", default=None, help="Output path for --json")
    return ap.parse_args()

def in_window(epoch: float, start: dt.date, end: dt.date) -> bool:
    return start <= dt.date.fromtimestamp(epoch) <= end

def extract_user_text(content):
    if isinstance(content, str): return content
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                return c.get("text", "")
            if isinstance(c, dict) and c.get("type") == "tool_result":
                return "[tool_result]"
    return ""

def collect(root, start, end):
    """Walk JSONL files and collect all assistant messages with usage."""
    files = glob.glob(os.path.join(root, "**/*.jsonl"), recursive=True)
    # Loose pre-filter by mtime: keep any file whose mtime is on/after window start.
    # A file modified after window-start may still contain in-window messages; the per-message
    # timestamp filter inside the loop is the actual gate. Files written before window start
    # cannot contain messages inside the window.
    start_epoch = dt.datetime.combine(start, dt.time.min).timestamp()
    files = [f for f in files if os.path.getmtime(f) >= start_epoch]

    # raw event list for later aggregation
    # event: {date, model, cost, sid, is_sub, agent_name, skill, ts}
    main_events = []  # main session messages
    sub_events  = []  # subagent messages
    sessions = {}     # sid -> {title, cwd, gitBranch}
    sub_meta = {}     # subagent_jsonl_path -> {agent_label, parent_sid}
    detected_session_skill = {}  # sid -> first-detected skill from user prompts
    fallback_title = {}  # sid -> first-real-user-msg title (used when no custom-title and no /rename)
    user_msg_idx = defaultdict(int)  # sid -> count of user msgs seen so far (cap detection to first N)
    # turn-level: per session, list of (ts, type=user|asst, text, cost, tools, model, attribution)
    turns_raw = defaultdict(list)

    for path in files:
        is_subagent = "/subagents/" in path
        try:
            with open(path) as f:
                # for subagents, capture the first user prompt to detect agent name
                first_user_seen = False
                detected_agent = None
                parent_sid = None
                for line in f:
                    try: d = json.loads(line)
                    except: continue
                    t = d.get("type")
                    if t == "custom-title":
                        sid = d.get("sessionId")
                        if sid:
                            sessions.setdefault(sid, {})
                            sessions[sid]["title"] = d.get("customTitle")
                        continue

                    sid = d.get("sessionId")
                    if sid and sid not in sessions:
                        sessions[sid] = {"title": None, "cwd": d.get("cwd"), "gitBranch": d.get("gitBranch")}

                    if t == "user":
                        msg = d.get("message", {})
                        c = msg.get("content") if isinstance(msg, dict) else None
                        text = extract_user_text(c)
                        ts = d.get("timestamp")
                        # /rename command sets a human-friendly title for the session;
                        # later renames overwrite earlier ones (last-write-wins).
                        if not is_subagent and sid and text and "<command-name>/rename</command-name>" in text:
                            m = re.search(r"<command-args>([^<]+)</command-args>", text)
                            if m:
                                sessions.setdefault(sid, {})
                                sessions[sid]["title"] = m.group(1).strip()
                        if not is_subagent and sid:
                            user_msg_idx[sid] += 1
                            # Capture a fallback title from the first real user message (not system noise).
                            if sid not in fallback_title and is_real_user_msg(text):
                                # Strip leading <command-*> wrappers, then take first 4 words of the first sentence/line.
                                clean = re.sub(r"<command-[^>]+>[^<]*</command-[^>]+>\s*", "", text).strip()
                                # First sentence: split on .!? newline
                                first_sentence = re.split(r"[.!?\n]", clean, 1)[0].strip()
                                words = first_sentence.split()
                                if len(words) <= 4:
                                    title = first_sentence
                                else:
                                    title = " ".join(words[:4])
                                if title:
                                    fallback_title[sid] = title[:60]
                        # Detect main-session skill from any of the first ~5 user messages.
                        # Skills can appear via /command-name, "Base directory for this skill: …",
                        # "Read and follow instructions from .claude/skills/X/", or a leading slash-command.
                        if not is_subagent and sid and sid not in detected_session_skill and text and user_msg_idx[sid] <= 5:
                            detected = None
                            m = re.search(r"<command-name>/?([\w-]+)</command-name>", text)
                            if m:
                                detected = m.group(1)
                            if not detected:
                                m = re.search(r"Base directory for this skill:\s*\S*?/skills/([\w-]+)/", text)
                                if m: detected = m.group(1)
                            if not detected:
                                m = re.search(r"\.claude/skills/([\w-]+)/", text)
                                if m: detected = m.group(1)
                            if not detected:
                                # Leading slash-command, e.g. "/falai do X". Reject if it looks like a file path
                                # ("/Users/...", "/tmp/...", "/etc/..." — the next char is "/" or contains "."/length>20).
                                stripped = text.lstrip()
                                # Only the first line, only when no "/" or "." or whitespace follows immediately,
                                # and word is reasonably short (slash-commands are typically <=20 chars).
                                m = re.match(r"/([a-z][\w-]{2,19})(?:\s|$)", stripped.split("\n",1)[0])
                                if m:
                                    detected = m.group(1)
                            if detected and detected not in ("clear","compact","help","cost","context","model","login","logout","status","config","effort","resume","init","quit","exit","ide"):
                                detected_session_skill[sid] = detected
                        if is_subagent and not first_user_seen and text:
                            first_user_seen = True
                            # parse "READ /…/agents/<name>.md" or "Base directory for this skill: /…/skills/<name>/"
                            m = re.search(r"agents/([\w-]+)\.md", text)
                            if m:
                                detected_agent = m.group(1)
                            elif re.search(r"skills/([\w-]+)/", text):
                                detected_agent = f"skill:{re.search(r'skills/([\\w-]+)/', text).group(1)}"
                            elif text.startswith("Your task is to create a detailed summary of the conversation"):
                                detected_agent = "auto-compact"
                            # else: leave None → falls back to general-purpose / attributionAgent
                            parent_sid = d.get("sessionId")
                        if not is_subagent and sid:
                            turns_raw[sid].append({
                                "type": "user",
                                "ts": ts,
                                "text": text[:200],
                            })
                        continue

                    if t != "assistant": continue
                    msg = d.get("message", {})
                    if not isinstance(msg, dict): continue
                    usage = msg.get("usage") or {}
                    if not usage: continue

                    ts = d.get("timestamp")
                    msg_date = None
                    if ts:
                        try:
                            msg_date = dt.datetime.fromisoformat(ts.replace("Z","+00:00")).date()
                            if not (start <= msg_date <= end): continue
                        except: pass

                    model = msg.get("model", "unknown")
                    c = cost_for(usage, model)
                    in_tok = usage.get("input_tokens", 0) or 0
                    out_tok = usage.get("output_tokens", 0) or 0
                    cr_tok = usage.get("cache_read_input_tokens", 0) or 0
                    cw_tok = usage.get("cache_creation_input_tokens", 0) or 0

                    # tools used in this assistant message
                    tools = []
                    asst_text = ""
                    content = msg.get("content")
                    if isinstance(content, list):
                        for cc in content:
                            if not isinstance(cc, dict): continue
                            if cc.get("type") == "tool_use":
                                tools.append(cc.get("name"))
                            elif cc.get("type") == "text" and not asst_text:
                                asst_text = cc.get("text", "")[:120]

                    base = {
                        "date": (msg_date or dt.date.today()).isoformat(),
                        "model": model_key(model),
                        "cost": c,
                        "in": in_tok, "out": out_tok, "cr": cr_tok, "cw": cw_tok,
                        "sid": sid,
                        "ts": ts,
                    }

                    if is_subagent:
                        # Prefer the agent file detected from the first user prompt,
                        # because attributionAgent is generic ("general-purpose") for many critic agents.
                        agent = detected_agent or d.get("attributionAgent") or "general-purpose"
                        base["agent"] = agent
                        base["parent_sid"] = parent_sid or sid
                        sub_events.append(base)
                    else:
                        skill = d.get("attributionSkill") or "(no-skill)"
                        base["skill"] = skill
                        main_events.append(base)
                        if sid:
                            turns_raw[sid].append({
                                "type": "asst",
                                "ts": ts,
                                "cost": c,
                                "tools": tools,
                                "skill": skill,
                                "text": asst_text,
                                "in": in_tok,
                                "cr": cr_tok,
                                "cw": cw_tok,
                                "model": model_key(model),
                            })
                # store sub meta after walking the file
                if is_subagent and parent_sid and detected_agent:
                    sub_meta[path] = {"agent": detected_agent, "parent_sid": parent_sid}
        except Exception:
            continue

    # Apply fallback_title for sessions that have no custom-title yet
    for sid, ftitle in fallback_title.items():
        if sid not in sessions:
            sessions[sid] = {}
        if not sessions[sid].get("title"):
            sessions[sid]["title"] = ftitle
    return main_events, sub_events, sessions, turns_raw, detected_session_skill

SYSTEM_PREFIXES = (
    "[tool_result]", "<command-", "<task-notification>", "<system-reminder>",
    "<local-command-", "Caveat:", "Base directory for this skill",
)

def is_real_user_msg(txt: str) -> bool:
    if not txt: return False
    for p in SYSTEM_PREFIXES:
        if txt.startswith(p): return False
    return True

def aggregate_turns(turns_raw):
    """Group raw events into user-turns. Each turn = one real user message + all asst replies until next real user message."""
    out = {}
    for sid, evts in turns_raw.items():
        evts = sorted([e for e in evts if e.get("ts")], key=lambda x: x["ts"])
        turns = []
        cur = None
        for e in evts:
            if e["type"] == "user":
                txt = e.get("text", "")
                if is_real_user_msg(txt):
                    if cur: turns.append(cur)
                    cur = {
                        "user": txt[:160],
                        "ts": e["ts"],
                        "asst_cost": 0.0,
                        "asst_msgs": 0,
                        "tools": defaultdict(int),
                        "skills": set(),
                        "first_asst_text": "",
                        "max_ctx": 0,
                        "models": defaultdict(int),
                    }
            elif e["type"] == "asst":
                if cur is None:
                    cur = {"user": "(session start)", "ts": e["ts"], "asst_cost": 0.0, "asst_msgs": 0, "tools": defaultdict(int), "skills": set(), "first_asst_text": "", "max_ctx": 0, "models": defaultdict(int)}
                cur["asst_cost"] += e["cost"]
                cur["asst_msgs"] += 1
                # context size at this assistant message: input + cache_read + cache_creation
                ctx = (e.get("in",0) or 0) + (e.get("cr",0) or 0) + (e.get("cw",0) or 0)
                if ctx > cur["max_ctx"]:
                    cur["max_ctx"] = ctx
                if e.get("model"):
                    cur["models"][e["model"]] += 1
                for tn in e.get("tools", []):
                    cur["tools"][tn] += 1
                if e.get("skill"): cur["skills"].add(e["skill"])
                if not cur["first_asst_text"] and e.get("text"):
                    cur["first_asst_text"] = e["text"]
        if cur: turns.append(cur)
        # serialize: drop zero-cost turns and "(session start)" placeholders
        out[sid] = [
            {
                "user": t["user"],
                "ts": t["ts"],
                "cost": round(t["asst_cost"], 4),
                "msgs": t["asst_msgs"],
                "tools": dict(t["tools"]),
                "skills": sorted([s for s in t["skills"] if s and s != "(no-skill)"]),
                "first_asst_text": t["first_asst_text"],
                "max_ctx": t.get("max_ctx", 0),
                "model": max(t.get("models", {}).items(), key=lambda x: x[1])[0] if t.get("models") else None,
            }
            for t in turns
            if t["asst_cost"] >= 0.01 and t["user"] != "(session start)"
        ]
    return out

def build_json_export(start, end, root):
    main_events, sub_events, sessions, turns_raw, detected_session_skill = collect(root, start, end)
    turns = aggregate_turns(turns_raw)

    # per-day rollup
    by_day = defaultdict(lambda: {"cost": 0.0, "msgs": 0, "in": 0, "out": 0, "cr": 0, "cw": 0})
    for e in main_events + sub_events:
        d = by_day[e["date"]]
        d["cost"] += e["cost"]
        d["msgs"] += 1
        d["in"]   += e["in"]
        d["out"]  += e["out"]
        d["cr"]   += e["cr"]
        d["cw"]   += e["cw"]

    # per-day-and-skill: attribute via session main_skill (filled below after sessions_out is built)
    day_skill = defaultdict(lambda: defaultdict(float))  # date -> skill -> $
    day_agent = defaultdict(lambda: defaultdict(float))  # date -> agent -> $
    for e in sub_events:
        day_agent[e["date"]][e.get("agent", "unknown")] += e["cost"]

    # session aggregates
    sess_agg = defaultdict(lambda: {"cost": 0.0, "msgs": 0, "skills": set(), "agents": set(), "subagent_cost": 0.0, "first_ts": None, "last_ts": None})
    for e in main_events:
        s = sess_agg[e["sid"]]
        s["cost"] += e["cost"]
        s["msgs"] += 1
        if e.get("skill"): s["skills"].add(e["skill"])
        if e["ts"]:
            if not s["first_ts"] or e["ts"] < s["first_ts"]: s["first_ts"] = e["ts"]
            if not s["last_ts"]  or e["ts"] > s["last_ts"]:  s["last_ts"]  = e["ts"]
    for e in sub_events:
        sid = e.get("parent_sid") or e["sid"]
        s = sess_agg[sid]
        s["cost"] += e["cost"]
        s["subagent_cost"] += e["cost"]
        s["agents"].add(e["agent"])

    sessions_out = []
    for sid, s in sess_agg.items():
        if s["cost"] < 0.01: continue
        meta = sessions.get(sid, {})
        non_meta = [k for k in s["skills"] if k and not k.startswith("superpowers:") and k != "(no-skill)"]
        if non_meta:
            main_skill = non_meta[0]
        elif sid in detected_session_skill:
            main_skill = detected_session_skill[sid]   # detected from /command-name in user prompt
        elif "superpowers:using-superpowers" in s["skills"]:
            main_skill = "superpowers:using-superpowers"
        else:
            main_skill = "(no skill)"
        sessions_out.append({
            "sid": sid,
            "title": meta.get("title") or sid[:8],
            "cost": round(s["cost"], 4),
            "subagent_cost": round(s["subagent_cost"], 4),
            "msgs": s["msgs"],
            "main_skill": main_skill,
            "skills": sorted(s["skills"]),
            "agents": sorted(s["agents"]),
            "first_ts": s["first_ts"],
            "last_ts": s["last_ts"],
            "date": (s["first_ts"] or "")[:10] or dt.date.today().isoformat(),
        })
    sessions_out.sort(key=lambda x: -x["cost"])

    # build day_skill via session main_skill lookup
    sid_to_main = {s["sid"]: s["main_skill"] for s in sessions_out}
    for e in main_events:
        ms = sid_to_main.get(e["sid"], "(no skill)")
        day_skill[e["date"]][ms] += e["cost"]

    # totals
    total_cost = sum(e["cost"] for e in main_events + sub_events)

    # by_agent: sum across subagents
    # by_skill: aggregate per session by its main_skill (so /blog-post sessions count fully)
    by_skill = defaultdict(float)
    by_agent = defaultdict(float)
    by_model = defaultdict(float)
    for s in sessions_out:
        by_skill[s["main_skill"]] += s["cost"] - s["subagent_cost"]
    for e in sub_events:
        by_agent[e.get("agent", "unknown")] += e["cost"]
    for e in main_events + sub_events:
        by_model[e["model"]] += e["cost"]

    return {
        "generated_at": dt.datetime.utcnow().isoformat() + "Z",
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "total_cost": round(total_cost, 2),
        "total_msgs": len(main_events) + len(sub_events),
        "by_day": {d: {**v, "cost": round(v["cost"],4)} for d, v in sorted(by_day.items())},
        "day_skill": {d: {k: round(v,4) for k,v in vs.items()} for d, vs in day_skill.items()},
        "day_agent": {d: {k: round(v,4) for k,v in vs.items()} for d, vs in day_agent.items()},
        "by_skill": {k: round(v,4) for k,v in sorted(by_skill.items(), key=lambda x:-x[1])},
        "by_agent": {k: round(v,4) for k,v in sorted(by_agent.items(), key=lambda x:-x[1])},
        "by_model": {k: round(v,4) for k,v in sorted(by_model.items(), key=lambda x:-x[1])},
        "sessions": sessions_out,
        "turns": turns,
    }

def print_text_report(start, end, root):
    main_events, sub_events, sessions, _, _ = collect(root, start, end)
    by_skill = defaultdict(float)
    by_agent = defaultdict(float)
    by_model = defaultdict(float)
    by_session = defaultdict(lambda: {"cost":0.0,"msgs":0,"skills":set(),"agents":set(),"title":None})
    for e in main_events:
        by_skill[e.get("skill","(no-skill)")] += e["cost"]
        by_model[e["model"]] += e["cost"]
        s = by_session[e["sid"]]
        s["cost"] += e["cost"]; s["msgs"] += 1
        if e.get("skill"): s["skills"].add(e["skill"])
        s["title"] = sessions.get(e["sid"], {}).get("title")
    for e in sub_events:
        by_agent[e.get("agent","unknown")] += e["cost"]
        by_model[e["model"]] += e["cost"]
        s = by_session[e.get("parent_sid") or e["sid"]]
        s["cost"] += e["cost"]
        s["agents"].add(e["agent"])
    total = sum(by_model.values())

    period = f"{start} → {end}" if start != end else f"{start}"
    print(f"\n=== USAGE BREAKDOWN  ({period}) ===")
    print(f"Total: ${total:.2f}\n")
    print("--- BY MODEL ---")
    for k,v in sorted(by_model.items(), key=lambda x:-x[1]):
        print(f"  {k:<24} ${v:>8.2f}")
    print("\n--- BY SUBAGENT ---")
    for k,v in sorted(by_agent.items(), key=lambda x:-x[1])[:25]:
        print(f"  {k:<32} ${v:>7.2f}")
    print("\n--- BY SKILL ---")
    for k,v in sorted(by_skill.items(), key=lambda x:-x[1])[:25]:
        print(f"  {k:<32} ${v:>7.2f}")
    print("\n--- TOP 15 SESSIONS ---")
    for sid, v in sorted(by_session.items(), key=lambda x:-x[1]["cost"])[:15]:
        if v["cost"] < 0.01: continue
        label = (v["title"] or sid)[:38]
        items = sorted(v["skills"]) + [f"@{a}" for a in sorted(v["agents"])]
        print(f"  {label:<40} ${v['cost']:>6.2f}  {v['msgs']:>4} msgs  {', '.join(items)[:55]}")

def main():
    args = parse_args()
    if args.date:
        d = dt.date.fromisoformat(args.date)
        start = end = d
    else:
        end = dt.date.today()
        start = end - dt.timedelta(days=args.days - 1)

    if args.json:
        data = build_json_export(start, end, args.root)
        out = args.out or "workspace/usage-dashboard/data.json"
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w") as f:
            json.dump(data, f, separators=(",",":"))
        print(f"Wrote {out}  ({os.path.getsize(out)/1024:.1f} KB)  total ${data['total_cost']}  sessions={len(data['sessions'])}  turns={sum(len(v) for v in data['turns'].values())}")
    else:
        print_text_report(start, end, args.root)

if __name__ == "__main__":
    main()
