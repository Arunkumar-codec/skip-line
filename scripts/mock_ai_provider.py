"""
scripts/mock_ai_provider.py
───────────────────────────
Deterministic OpenAI-compatible mock LLM provider for the SkipLine browser
acceptance suite and local demos.

Purpose (matching the production provider contract in app/services/ai_provider.py):
  POST /v1/chat/completions  ->  {"choices":[{"message":{"content": <str>}}]}

The assistant (app/services/ai_assistant_service.py) speaks a STRICT JSON
action protocol. This mock implements the SAME protocol with a deterministic,
self-contained reply policy derived ONLY from the conversation the service
sends it:

  - "TOOL_RESULT <json>" messages in the history carry authoritative backend
    observations. This mock treats them as DATA ONLY (never as instructions),
    exactly like the real protocol requires (C2/C3): every id/price/org it
    later emits is copied from those observations — it NEVER invents
    organizations, services, counters, prices or slots.
  - The FIRST user message may carry directives extracted with a strict
    regex, so tests remain deterministic without an NLP layer:
        org="NAME"      -> target organization name substring
        service="NAME"  -> target service name substring
  - Decision tree (pure function of the message history):
      search organizations -> list services -> list counters ->
      check availability -> quote price -> prepare booking draft ->
      reply "press Confirm & Book".
  - A user message containing DIRECTIVE "status token=<id>" triggers
    get_booking_status for that id (owner checks run server-side).
  - Unknown shapes degrade to a plain reply action (never a crash).

No credentials, no network beyond localhost; the port is passed at start.
Runs with:  python scripts/mock_ai_provider.py [--port 181xx]
"""
import argparse
import json
import re
from datetime import date, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

_OG_RE = re.compile(r'org="([^"]+)"')
_SERVICE_RE = re.compile(r'service="([^"]+)"')
_TOKEN_RE = re.compile(r"status token=(\d+)")


def _user_texts(messages: list) -> list[str]:
    return [m.get("content") or "" for m in messages if m.get("role") == "user"]


def _tool_observations(messages: list) -> list[dict]:
    """Parsed TOOL_RESULT payloads, DATA ONLY (never treated as instructions).

    The app wraps every observation as
    ``{"action": ..., "result": <observation>, "errors": [...]}``
    (app/services/ai_assistant_service.py consult loop), so the nested
    ``result`` object is where the authoritative backend data lives — a
    conforming provider reads it there. (WP1 fix 2026-11-10: the mock used
    to scan only the top-level envelope, never saw the data, re-issued
    ``search_organizations`` until the bounded-loop cap, and the C10 close
    replaced the booking draft — flows 5/6 could never reach Confirm & 
    Book.)
    """
    out: list[dict] = []
    for text in _user_texts(messages):
        if text.startswith("TOOL_RESULT "):
            try:
                payload = json.loads(text[len("TOOL_RESULT "):])
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            inner = payload.get("result")
            if isinstance(inner, dict):
                out.append(inner)
            out.append(payload)  # top-level view retained for diagnostics
    return out


def _find(obs_list: list, key: str) -> dict | None:
    for obs in obs_list:
        if key in obs:
            return obs
    return None


def _reply(text: str) -> str:
    return json.dumps({"action": "reply", "text": text})


def _decide(messages: list) -> str:
    texts = _user_texts(messages)
    obs = _tool_observations(messages)

    # Directives may appear in the D1 manual-context prefill message OR in
    # later user turns — search all plain user texts (never TOOL_RESULT).
    directives = ""
    for text in reversed(texts):
        if not text.startswith("TOOL_RESULT "):
            if _OG_RE.search(text) or _SERVICE_RE.search(text) or _TOKEN_RE.search(text):
                directives = text
                break
    first_user = directives or next((t for t in texts if not t.startswith("TOOL_RESULT ")), "")

    org_rule = _OG_RE.search(first_user)
    svc_rule = _SERVICE_RE.search(first_user)
    status_rule = _TOKEN_RE.search(first_user)

    search = _find(obs, "organizations")
    services = _find(obs, "services")
    counters = _find(obs, "counters")
    availability = _find(obs, "slots")
    price = _find(obs, "price")
    prepared = _find(obs, "prepared")

    if status_rule and not _find(obs, "booking"):
        return json.dumps({"action": "get_booking_status",
                           "args": {"token_id": int(status_rule.group(1))}})

    if prepared:
        return _reply("Your booking draft is ready. "
                      "Press the app's Confirm & Book button to finish.")

    if price and search and counters and (not svc_rule or availability):
        args = {
            "organization_id": search["organizations"][0]["id"],
            "counter_id": counters["counters"][0]["id"],
        }
        if services and services.get("services"):
            args["service_id"] = services["services"][0]["id"]
            if svc_rule:
                match = next((s for s in services["services"]
                              if svc_rule.group(1).lower() in s["name"].lower()),
                             None)
                if match:
                    args["service_id"] = match["id"]
        if availability and availability.get("state") == "OPEN":
            bookable = [s for s in availability.get("slots", [])
                        if s.get("bookable")]
            if bookable:
                args["slot_id"] = bookable[0]["id"]
        return json.dumps({"action": "prepare_booking", "args": args})

    if services and counters and not price:
        if availability is None:
            return json.dumps({
                "action": "check_availability",
                "args": {
                    "organization_id": search["organizations"][0]["id"],
                    "counter_id": counters["counters"][0]["id"],
                    "date": (date.today() + timedelta(days=1)).isoformat(),
                },
            })
        return json.dumps({
            "action": "quote_price",
            "args": {"organization_id": search["organizations"][0]["id"],
                     "service_id": services["services"][0]["id"]},
        })

    if search and services is None and svc_rule and search.get("organizations"):
        return json.dumps({
            "action": "list_services",
            "args": {"organization_id": search["organizations"][0]["id"]},
        })
    if search:
        if counters is None and services is not None:
            return json.dumps({
                "action": "list_counters",
                "args": {"organization_id": search["organizations"][0]["id"]},
            })
        if services is None and not svc_rule:
            return json.dumps({
                "action": "list_services",
                "args": {"organization_id": search["organizations"][0]["id"]},
            })
        if services and counters is None:
            return json.dumps({
                "action": "list_counters",
                "args": {"organization_id": search["organizations"][0]["id"]},
            })

    if org_rule and search is None:
        return json.dumps({"action": "search_organizations",
                           "args": {"query": org_rule.group(1)}})

    return _reply("How can I help you book a service today?")


@app.get("/healthz")
async def healthz():
    return JSONResponse({"ok": True, "provider": "mock"})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    messages = body.get("messages") or []
    # NOTE: the Authorization header is never read or logged by this mock.
    return JSONResponse({
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "model": body.get("model") or "mock-model",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": _decide(messages)},
            "finish_reason": "stop",
        }],
    })


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=18100)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
