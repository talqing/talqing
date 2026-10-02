# Talqing Python SDK

The Python client for the Talqing `/v1` API, generated from `openapi/openapi.json`.
It is the same set of operations, under the same names, as the TypeScript SDK —
both are generated from the surface `backend/api/dataplane/sdk_surface.py`
declares.

`httpx` is the only runtime dependency. Responses come back as decoded JSON,
described by TypedDicts: nothing is validated, coerced or renamed on arrival.

## Install

```bash
pip install talqing
```

Python 3.10 or newer. The package ships a `py.typed` marker, so a type checker
in your project sees every TypedDict it declares.

To work against a checkout instead: `uv pip install -e clients/python`.

## Authentication

Create a personal access token in the dashboard under **Organization → API
Tokens**. The client sends it as `Authorization: Bearer <token>`.

```python
from talqing import Talqing

with Talqing(token="tq_...", base_url="https://api.in.talqing.com") as talqing:
    print(talqing.agents.list())
```

**One token reaches every region.** A token names your organization, not a
region, and every organization exists in every region — so working in a second
region is the same token and a different `base_url`. The resources are separate:
an agent you create in `https://api.in.talqing.com` is not in
`https://api.us.talqing.com`, and neither is its call history or its credit
balance.

`Talqing.from_env()` reads the same two values from `TALQING_API_KEY` and
`TALQING_BASE_URL`:

```bash
export TALQING_API_KEY="your-personal-access-token"
export TALQING_BASE_URL="http://localhost:8000"
```

```python
with Talqing.from_env() as talqing:
    print(talqing.agents.list())
```

`base_url` has no default. A client that quietly points at localhost fails in
production as a connection error three layers down; one that refuses to start
says what is actually wrong.

## The shape of it

Operations are reached by resource, exactly as the API names them:

```python
talqing.agents.list(limit=50)
talqing.agents.get(agent_id)
talqing.agents.versions.rollback(agent_id, 3)
talqing.calls.batches.pause(batch_id)
talqing.telephony.phone_numbers.assign(number_id, agent_id=agent_id)
```

Path parameters are positional; everything else — request body fields and query
parameters alike — is a keyword argument:

```python
agent = talqing.agents.create(
    config={"name": "Support bot", "channel": "text", "prompt": "Be concise."}
)
talqing.agents.update(agent["id"], config={**agent["config"], "greeting": None})
talqing.agents.publish(agent["id"])
```

**An argument you do not pass is not sent.** That is what makes a PATCH able to
say `null`:

```python
talqing.telephony.phone_numbers.update(number_id, label=None)  # clear the label
talqing.telephony.phone_numbers.update(number_id, can_outbound=False)  # leave it alone
```

The one name that is not the API's own is `telephony.phone_numbers.import_()` —
`import` is a Python keyword.

## Errors

Every non-2xx raises `TalqingAPIError`. There is one error shape, so there is
nothing to branch on:

```python
from talqing import TalqingAPIError

try:
    talqing.agents.publish(agent_id)
except TalqingAPIError as error:
    print(error.status_code, error)  # 400 config is invalid
    for problem in error.errors:  # ['llm: unknown model gpt-4.9']
        print(problem)
```

## Pagination

Every list endpoint pages the same way, so one helper covers all of them.
Anything else the endpoint filters on passes straight through:

```python
from talqing import paginate

for agent in paginate(talqing.agents.list):
    print(agent["config"]["name"])

for call in paginate(talqing.calls.list, agent_id=agent_id, type="SIP_INBOUND"):
    print(call["id"], call["cost"])
```

## Credits

The platform fee — charged per minute of voice or video, and on nothing else —
comes out of a prepaid balance. Provider spend is yours, on your own keys, and
never passes through Talqing.

**A workspace has one balance per region**, so this reads the balance belonging
to whichever `base_url` the client holds. A call placed with nothing left in
that region never connects, and reads back with
`close_reason == "insufficient_credits"`.

```python
credits = talqing.billing.credits.get()
print(credits["balance"], credits["currency"], credits["voice_minutes_remaining"])

for entry in paginate(talqing.billing.credits.ledger):
    print(entry["created_at"], entry["kind"], entry["amount"], entry["balance_after"])
```

Buying more is an admin's call, and returns a hosted checkout page to open in a
browser. The credit lands in the region you bought it from — balances do not
move between regions, so buy against the base URL you meant to top up.

```python
checkout = talqing.billing.credits.checkout(pack="usd_50")
print(checkout["checkout_url"])
```

## Live streams

Six endpoints stay open and push events. Each frame is decoded and repeats its
own name in `event`, which is what tells the frames apart:

```python
for event in talqing.conversations.events(conversation_id):
    if event["event"] == "assistant.delta":
        print(event["text"], end="", flush=True)
```

Use it as a context manager when the loop may exit early, so the connection
closes with it:

```python
with talqing.conversations.events(conversation_id) as events:
    for event in events:
        if event["event"] == "turn" and event["status"] == "done":
            break
```

## Text conversations

Text agents do not use LiveKit rooms. Start a chat, then send it messages — the
agent's reply comes back on the same call:

```python
chat = talqing.chats.create(agent_id=agent_id, contact_key="user-42")

turn = talqing.chats.messages.create(
    chat["id"], message="Hello", client_message_id=str(uuid4())
)
for item in turn["items"]:
    print(item["text"])

talqing.chats.end(chat["id"])
```

A chat stays open until its agent ends it or you call `chats.end`; that is when
its analysis runs and `session.completed` fires.

Voice and video agents take a LiveKit room token instead:

```python
token = talqing.calls.token(agent_id=agent_id)
print(token["server_url"], token["participant_token"])
```

## Async

`AsyncTalqing` has the same surface with every operation a coroutine. A stream
is the exception: it is not awaited on either client, so `async for` reads the
way `for` does.

```python
import asyncio
from talqing import AsyncTalqing, paginate_async


async def main() -> None:
    async with AsyncTalqing.from_env() as talqing:
        await talqing.agents.list()

        async for agent in paginate_async(talqing.agents.list):
            print(agent["id"])

        async for event in talqing.copilot.agents.stream(agent_id):
            print(event["event"])


asyncio.run(main())
```

## Types

Every request and response shape is exported from `talqing`, named as the API
names it:

```python
from talqing import AgentConfig, AgentResponse, CallOutcome, OperationRequest
```

A response is a plain `dict` at runtime — the TypedDicts describe it for your
type checker and your editor, and cost nothing when you run. That also means the
SDK can never reject a payload the API considers valid.

`Page[T]` is the shape every list endpoint returns:

```python
page = talqing.tools.list(limit=50)
page["items"], page["has_more"], page["limit"], page["offset"]
```

## Escape hatches

`talqing.request(...)` calls a path directly with this client's credentials and
error handling, for an endpoint that shipped since this SDK was generated.
`talqing.http` is the underlying `httpx.Client`, already carrying the base URL
and the token, for anything else.

`talqing.oauth_start_url(provider)` builds the browser redirect that authorizes
an integration — not an operation, so nothing generates it.

## Regenerating

`src/talqing/gen` is generated and checked in. After re-running
`openapi/export.py`:

```bash
python clients/python/generate.py            # rewrite it
python clients/python/generate.py --check    # or just assert it is current
```

Everything else in `src/talqing` is hand-written: the client, the transport, the
error, and the pagination helper — what the document cannot say.

```bash
cd clients/python && pytest && mypy
```
