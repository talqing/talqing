# Workers

All runtime processes live under `backend/workers/`. Domain logic stays in
packages like `services/` (including `services/messaging/`) and `compiler/`;
workers wire that domain into LiveKit / Kafka / Redis.

## Processes (docker-compose)

| Service | Module | Role |
|---|---|---|
| `voice-worker` | `python -m workers.voice.main start` | LiveKit room voice/video and SIP calls |
| `text-worker` | `python -m workers.text.main` | Kafka text turns: tenant agents and the CoPilots |
| `background-worker` | `python -m workers.background.main` | Every `scheduled_jobs` kind (see `../services/jobs/`) |

## Where do I look?

| I want to work on… | Open |
|---|---|
| Shared voice lifecycle (compile/transcript/finalize) | `voice/runtime.py` |
| Voice worker entry / dispatch | `voice/main.py` |
| Web voice / video room agents | `voice/room_web.py` + `voice/runtime.py` |
| SIP inbound / outbound | `voice/sip.py` + `../services/telephony/` |
| Conversation keys / refs / resolve | `../services/conversations/` |
| Text agents (web / Telegram) | `text/executor.py` + `text/tenant_executor.py` + `text/turn.py` + messaging inbound/outbound/typing (warm multi-turn, 60s idle) |
| CoPilot chat turns (agent / tool / task) | `text/copilot_executor.py` + `../services/copilot/` (shared conversation tables, no sessions) |
| Text step persist / SSE fan-out | `text/steps.py` + `text/fanout.py` |
| Text window build / flush (internal to a chat) | `text/tenant_executor.py`, `text/window.py` |
| Chat end: exit hook, analysis, bill, webhook | `text/end.py` |
| Session create / transcript / finalize | `session/` (`load`, `transcript`, `sessions`; facade `persistence`) |
| Compile definition → LiveKit AgentSession | `../compiler/` |
| Call dashboard list/detail/token | `../services/calls/` (not workers) |

## Layout

```
workers/
  voice/        # LiveKit AgentServer entry + runtime + room/sip paths
    main.py         # AgentServer + kind dispatch (web | sip_call)
    runtime.py      # VoiceRun shared lifecycle
    room_web.py     # web/video RoomIO path
    sip.py          # SIP inbound/outbound path
    client_rpc.py / background_audio.py / avatar*.py
  text/         # Kafka consumer + text turn path (see layout below)
  background/   # Redis queue consumers + billing loop
  session/      # shared tenant DB session I/O (not a process)
```

### Text worker layout

```
text/
  main.py                 # Kafka consumer process entry
  actor.py                # per-conversation serialize + idle close + interrupt
  executor.py             # load → prepare → run → park (orchestration only)
  tenant_executor.py      # cold-start published tenant text agents
  copilot_executor.py     # cold-start any CoPilot (subject comes from the plane)
  planes.py               # plane policy flags (bill, visibility, item source)
  types.py                # TextTurnInput, TextWindow, TextTurnOutcome
  load.py                 # load inbound item + platform bind_id
  interruption.py         # latest-wins supersede protocol
  turn.py                 # one LiveKit turn + progressive step workers
  steps.py                # persist chat items / seal / execution_error
  fanout.py               # SSE + provider delivery per plane
  window.py               # mint session id, finalize + bill, aclose
  events.py               # turn status SSE (running|done|error|canceled)
```

## Layering

- `api` must not import `workers.*`.
- Platform text-agent identity lives in `services.copilot.subjects` (not `utils/`).
- `compiler` may use `workers.session` (handoff loads published definitions).
- Channel adapters may call into `workers.voice` / `workers.session`.
