# AGENTS.md

## Talqing

We are building a no-code AI agent builder platform, with most focus on voice agents. Our goal is that any agent users can build by writing python livekit agents code, they should be able to build via our platform - either manually via dashboard or by talking to our CoPilots (AgentCoPilot in the agent editor, ToolCoPilot in the tool editor, TaskCoPilot in the task editor) or by connecting their Claude Code / Codex with our MCP. Thus, we should support majority of use-cases. We want absolute best user experience too, users should find every aspect of our platform correct and intuitve.

## Engineering Principles

We always strive to write great quality code, not just code that is functional. Readability, maintainability, and developer experience are of utmost importance. Never compromise on developer experience.

For frontend work, the bar is what OpenAI or Anthropic would ship — screenshot what you built and judge it, rather than checking that it rendered, because those are different acts and only the second one catches a screen that works and still looks cheap. Ask yourself is this the best design you could make? Do you imagine a trillion dollar company shipping that UI?

Prefer code that fails loudly during local development over code that behaves unpredictably in production. Avoid broad fallback logic such as `value || ""`, silent defaults, response field guessing, or swallowed errors unless the fallback is genuinely part of the intended product behavior.

Avoid extracting helper functions for very simple logic. When logic is straightforward and used locally, writing it inline is usually more readable than forcing readers to jump elsewhere.

When asked to review a section of the codebase, you need not worry about container restarts and DB write failures etc - focus on more important practical issues.

Always remember that we are at an MVP stage, introducing complexity for a rare edge case does not make sense at this stage.

Never take decisions that can hurt user experience or product experience or quality of AI agents being built on the platform without surfacing them to the humand and taking their approval. Every plan md file must being with asking the agent implementing the plan to read AGENTS.md

When solving a problem, always take a step back and also think about why the problem exists in the first place and whether a cleaner design just remove the problem's existence altogether. Think from first principles.

When reading code, if you notice any design smells, let human know, even if human didn't ask for it

Always strive to reduce technical debt, not increase it

We don't want our users looking at our product & api design and thinking that it's a non-serious hobby project. Everything should be correct and optimal and we should be able to defend our designs & decisions against criticism.

At the end of the day, our platform is a UI layer over livekit agents framework (+ additional plumbing to improve ease-of-use), and thus our design should reflect that. Whenever in a dilemma about product design, see how it's designed in ../agents. 

During product & system design, embody the spirit of a highly effective CTO of a tech company, who prioritizes users over anything else, and whose decisions can be defended if scrutinized later. If you identify a bad decision in code, even if not taken by you and taken by a prior agent session, highlight that too to human. Don't assume bad decisions to be written-in-stone that you should work under. That's how we would end up with a world class product after 1000 agent sessions.

## Writing plan md files

When creating a plan file, always better to ask questions instead of baking in bad assumptions that hurt product UX in any way.

When human and you are talking to refine / improve a plan md file under discussion, don't rush to answer. Always, research thoroughly, take your time, and come back with suggestions that are really in the best interest of the platform. Settling for less good platform isn't something we appreciate. 

We usually write plan md files for a fresh claude code session. So the plan file should have all the relevant information and references the fresh claude code session would need to implement the plan and should not contain any content that the fresh claude code session won't really need for the implementation. The plan should reflect the latest state of plan, and not a history of updates or research trail.

Our MCP token size is already big, when adding new routes or structs, make sure to keep docstrings & descriptions minimal (1-2 lines)

## Product UX Principles

For product UX, you may use the intersection of Vapi & ElevenLabs ElevenAgents as the bar. If either of them don't do something, chances are we wouldn't need to do that too. Intersection, not Union, because we don't want an overcomplicated product for MVP, but we still want a powerful and useful product. You may also use their OpenAPI spec for inspiration when designing our APIs 

Whenever in a dilemma, assume you are the CEO and have to balance both correctness & user-experience & MVP simplicity - ask yourself what would you do?

## Minor notes

Any SELECT query to tenant DB must have tenant_id in WHERE claude

To rebuild application: docker compose down && docker compose up --build

Every Python container reaches Postgres through the `pgbouncer` service, because `TALQING_PG_PROXY` is set in `backend/.env`. A `psql` aimed at `control-pg` / `data-pg` (or at host ports 5433 / 5434) bypasses it, which is correct and intended — the proxy is for the applications. `pgbouncer/pgbouncer.ini` explains the sizing, and unsetting `TALQING_PG_PROXY` is the rollback.

If you make changes to typescript client, you would need to rebuild it

When making frontend/ changes, don't run npm run build as that makes `npm run dev` stop working. We always have a `npm run dev` running and you can inspect frontend in google chrome via chrome-devtools-mcp

If are about to git commit and you notice changes in this file, including this file in git commit too.

If you add anything in .env.example, also add it in .env

Before git commit always read and review the git diff one last time to make sure everything looks correct

If you need to ask the human a doubt, notify them with `terminal-notifier -message "whatever message"` (then ask the full question in your response)

## Test calls in dashboard
You may create test agents and tools and anything else needed to test your code changes. If behaviour can be tested with text agents, do that. If it requires voice or video agents, then loop the human in for human side of the conversation of test call. If chrome-devtools-mcp hangs, let the human know so they can approve the remote debugging dialog that pops up.


## Livekit Information
- Livekit Agents repo is cloned at ../agents
- Livekit repo is cloned at ../livekit
- Livekit SIP repo is cloned at ../sip
- Livekit browser SDK (`livekit-client`) is cloned at ../client-sdk-js
- Livekit Python SDKs (`livekit.rtc`, `livekit.api`) are cloned at ../python-sdks
- You may access livekit docs

## Kamailio Information
- Kamailio is cloned at ../kamailio

## Provider APIs
To validate a doubt about a provider's API, call it for real with a key from your local config (`backend/configs/local.in.config.yaml`) instead of guessing
