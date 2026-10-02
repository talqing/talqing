import type { Metadata } from "next";

import {
  Bullets,
  DataTable,
  LegalPage,
  Note,
  Subheading,
  type LegalSection,
} from "../legal-page";
import { LEGAL_ENTITY, SUPPORT_EMAIL } from "../marketing-chrome";

export const metadata: Metadata = {
  title: "Privacy policy — Talqing",
  description:
    "What Talqing collects, who else sees it, where it is stored, and how long we keep it.",
  alternates: { canonical: "/privacy" },
  robots: { index: true, follow: true },
};

const UPDATED = "3 October 2026";

const DOCS = "https://docs.talqing.com";

// Written against the platform rather than against a template, which means two
// standing obligations on whoever edits this file next:
//
//  1. Every factual claim here is checkable in the codebase. When the behaviour
//     changes, this page changes in the same commit. The paragraph this policy
//     used to carry — "we do not record or store call audio or video" — was true
//     when it was written and stayed on the site for a month after recording
//     shipped on by default. That is the failure mode to design against.
//  2. Section 4's split between "providers you connect" and "our processors" is
//     load-bearing, not presentational. Strict BYOK is what makes a model
//     vendor the customer's processor and not ours, so a feature that puts a
//     platform key behind an agent run moves that row across the boundary and
//     changes what we are legally saying.
//
// `documentation/platform/data-retention-and-privacy.mdx` is the engineering
// half of the same story and must not disagree with this page.
const sections: LegalSection[] = [
  {
    id: "who",
    heading: "Who we are, and who this covers",
    body: (
      <>
        <p>
          Talqing is a platform for building and running AI voice, video and text
          agents. It is operated by {LEGAL_ENTITY.name} (CIN {LEGAL_ENTITY.cin}),{" "}
          {LEGAL_ENTITY.address} — &ldquo;Talqing&rdquo;, &ldquo;we&rdquo;,
          &ldquo;us&rdquo;.
        </p>
        <p>
          This policy covers <strong>talqing.com</strong>, our documentation at{" "}
          <strong>docs.talqing.com</strong>, the Talqing dashboard at{" "}
          <strong>app.talqing.com</strong>, and the Talqing API.
        </p>
        <p>
          Two different groups of people appear in it, and they are not treated
          the same way:
        </p>
        <Bullets
          items={[
            <>
              <strong>Customers</strong> — the people and organisations who hold a
              Talqing account and build agents. For their data we are the
              controller, and this policy is the full description of what we do.
            </>,
            <>
              <strong>End users</strong> — the people who call, message or talk to
              an agent that a customer has built and published. For their data we
              are a processor acting on that customer&rsquo;s instructions. The
              customer decides what is collected, what the agent says, whether the
              call is recorded and how long any of it is kept; their own privacy
              notice governs it, not this one.
            </>,
          ]}
        />
        <Note heading="If you spoke to an agent and want your data removed">
          <p>
            Contact the business that operates it — they can delete the recording
            and the transcript themselves, and we cannot do it without their
            instruction. If you cannot identify or reach them, write to{" "}
            {SUPPORT_EMAIL} with the number you called and roughly when, and we
            will route the request to the workspace that owns the agent.
          </p>
        </Note>
      </>
    ),
  },

  {
    id: "collect",
    heading: "What we collect",
    body: (
      <>
        <p>
          <strong>Account data.</strong> Sign-in is Google OAuth only — there is
          no password to store, and we never see one. From your Google profile we
          keep your email address, your Google account identifier, your display
          name and your profile picture URL, refreshed each time you sign in. We
          also keep which workspaces you belong to and your role in each, and a
          record of every personal access token you create — the token text
          itself is not stored, only the record that authorises it.
        </p>
        <p>
          <strong>Workspace content.</strong> The agents you build and every
          published version of them: prompts, greetings, model choices, tools and
          their code, agent tasks, FAQs, webhook endpoints, and your workspace
          settings.
        </p>
        <p>
          <strong>Session records.</strong> For every agent run we store which
          agent and published version handled it, the channel (voice, video or
          text), start and end times, duration, how it ended, latency and usage
          counters, and the platform fee it was charged. For calls placed over the
          telephone network this includes the calling and called numbers in E.164
          form, and the destination of any transfer to a human.
        </p>
        <p>
          <strong>Conversation content.</strong> The transcript of each session as
          text — what the end user said, what the agent replied, every tool the
          agent invoked and everything those tools returned, the values the agent
          captured during the call, and any images sent into the conversation. We
          strip EXIF metadata, including GPS coordinates, from every image before
          storing it.
        </p>
        <p>
          <strong>Call recordings.</strong> Voice and video agents record by
          default. One stereo audio file per call — the caller on one channel, the
          agent on the other. If the agent watched a shared screen and its author
          asked for that to be kept, a second file holds the screen as low
          frame-rate video. Both live in object storage and are reachable by the
          workspace through links that expire after an hour. Section 8 covers
          recording and consent on its own.
        </p>
        <p>
          <strong>Post-call analysis.</strong> Unless the agent&rsquo;s author
          turns it off, the finished transcript of a voice or video call is passed
          once to a language model to produce a summary, optionally a judgement of
          whether the call succeeded, and any fields the workspace asked to
          extract. That model call runs on the customer&rsquo;s own provider key,
          not ours.
        </p>
        <p>
          <strong>What an agent remembers between calls.</strong> Each caller
          identity a workspace has spoken to — a phone number, a Telegram account,
          or an identifier the customer supplied — has a contact record that
          accumulates what the agent learned, so it recognises that person on the
          next call. It exists until the last conversation with that person is
          deleted.
        </p>
        <p>
          <strong>Recipient lists.</strong> An outbound call batch holds the phone
          numbers a customer uploaded and whatever per-person fields they attached.
          An email batch holds email addresses, the message drafted for each
          recipient and its delivery result.
        </p>
        <p>
          <strong>Credentials you give us.</strong> Provider API keys you bring,
          OAuth tokens for apps you connect, telephony account credentials,
          messaging bot tokens, and any secrets you store for your tools. These are
          encrypted at rest and are never returned to the dashboard, the API, a
          model or a transcript after you save them.
        </p>
        <p>
          <strong>Technical data.</strong> Our servers log IP addresses, request
          paths, timestamps and error traces, and emit operational metrics. Both
          the logging and the metrics stack are our own; neither is sent to a
          third-party service.
        </p>
        <p>
          <strong>Payment data.</strong> Card details never reach us — checkout is
          hosted by our payment processor. We pass them your email address and
          name, and we keep the record of which credit pack was bought, what it
          cost and whether it was paid.
        </p>
      </>
    ),
  },

  {
    id: "use",
    heading: "What we use it for",
    body: (
      <>
        <Bullets
          items={[
            "Running the service — authenticating you, executing your agents, connecting calls, and sending each turn to the providers your agent is configured to use.",
            "Showing you your own data — transcripts, recordings, call history, analysis, usage and cost in the dashboard.",
            "Billing — metering platform-fee usage against your credit balance.",
            "Security, debugging and abuse investigation — diagnosing errors and outages, and looking into misuse of the platform.",
            "Support, when you ask us for it.",
            "Service messages — changes that affect your account, your agents or this policy.",
          ]}
        />
        <p>
          <strong>
            We do not sell your data, and we do not use your conversations or
            recordings to train models.
          </strong>{" "}
          We have no models of our own to train, and the providers your agents run
          on do so under your account and your settings with them rather than ours
          — if you have turned training off in your provider&rsquo;s console, that
          is the setting in force. We run no advertising, and there is no
          advertising or analytics tracker anywhere on this site or in the
          dashboard.
        </p>
      </>
    ),
  },

  {
    id: "third-parties",
    heading: "Who else sees it",
    body: (
      <>
        <p>
          There are two kinds of third party here, and treating them as one list
          would misdescribe how the platform works.
        </p>

        <Subheading>Providers you connect yourself</Subheading>
        <p>
          Talqing is strict bring-your-own-keys: no credential of ours ever backs
          an agent run. Every language model, speech-to-text, text-to-speech and
          avatar an agent runs is called with <strong>your</strong> key, on{" "}
          <strong>your</strong> account, and that provider bills you directly. The
          same is true of the carrier that carries a phone call, the apps you
          connect for tools, and the messaging channel you attach.
        </p>
        <p>
          For all of these we transmit data on your instruction, and we hold no
          relationship with them on your behalf. Your own agreement with each
          provider is what governs their handling of it, including whatever you
          have configured there about retention and training. We cannot read or
          override those settings.
        </p>
        <p>
          Which of them sees a given conversation depends entirely on how that
          agent is configured. An agent with no phone number never touches a
          carrier; an agent with no integrations never leaves its model stack.
        </p>
        <DataTable
          columns={["Category", "Providers"]}
          rows={[
            [
              "Language, speech-to-text and text-to-speech models",
              "OpenAI, Google (Gemini), xAI, Deepgram, ElevenLabs, Sarvam AI, Soniox, Raya",
            ],
            ["Video avatars", "Anam"],
            [
              "Noise suppression",
              "ai-coustics — the model runs on our servers under your licence, so the audio itself does not leave",
            ],
            ["Telephony carriers", "Exotel, Plivo, Twilio, Vobiz"],
            ["Messaging channels", "Telegram"],
            [
              "Apps you connect for tools",
              "Google Calendar, Cal.com, Calendly, Asana, Jira (Atlassian), HubSpot, RocketReach, Exa, Tavily, Resend",
            ],
            [
              "Anywhere you point an agent",
              "A custom MCP server, an HTTP or code tool, or a webhook endpoint — each reaches the host you name and nothing else",
            ],
          ]}
        />
        <p>
          Some models also offer built-in tools — web search, code execution, file
          search. Enabling one lets that model&rsquo;s provider act on the
          conversation on their own infrastructure, under the same agreement of
          yours.
        </p>

        <Subheading>Our own processors</Subheading>
        <p>
          These we chose, and they act for us under our contracts. This is the
          complete list.
        </p>
        <DataTable
          columns={["Processor", "What they do for us"]}
          rows={[
            [
              "DigitalOcean",
              "Compute, databases and object storage in every region",
            ],
            [
              "Cloudflare",
              "DNS, and hosting for the public website and the documentation",
            ],
            [
              "Google",
              "Sign-in over OpenID Connect, and the mailbox that receives support, privacy and legal requests",
            ],
            ["Dodo Payments", "Hosted checkout and payment processing for credit packs"],
            [
              "Calendly",
              "Scheduling a demo call, if you book one on its site — it receives what you type into the booking form",
            ],
            [
              "OpenAI",
              "The CoPilots in the editors — never an agent run",
            ],
          ]}
        />
        <p>
          That last row is the one exception to bring-your-own-keys, so it is worth
          stating plainly. The CoPilots read the workspace configuration you are
          editing and can call the same API you can. They run on our own OpenAI
          account. Nothing about a live agent run — no call, no message, no task —
          ever does.
        </p>
        <p>
          We disclose data outside these two lists only where the law requires it,
          or to protect the rights and safety of our users — and we will tell you
          unless we are legally barred from doing so.
        </p>
      </>
    ),
  },

  {
    id: "where",
    heading: "Where your data lives",
    body: (
      <>
        <p>
          Talqing runs one global control plane and two independent regions. You
          choose a region by which API you call. Nothing is copied or replicated
          between them, and no screen in the product merges them.
        </p>
        <DataTable
          columns={["What", "Where it is stored"]}
          rows={[
            [
              "Identity, workspace membership, access tokens and payment records",
              "Frankfurt, Germany",
            ],
            [
              "India region — agents, calls, transcripts, analysis, credit balance",
              "Bengaluru, India",
            ],
            [
              "India region — call recordings and image attachments",
              "Singapore",
            ],
            [
              "United States region — everything a workspace owns there",
              "San Francisco, United States",
            ],
          ]}
        />
        <Note heading="The Singapore row is a limitation, not a preference">
          <p>
            Our object storage provider does not currently offer buckets in
            Bengaluru. Recordings and image attachments belonging to Indian
            workspaces are therefore held in the nearest datacentre it does offer,
            in Singapore. Transcripts, analysis, contact records and every other
            row for those workspaces stay in India. We would rather say this than
            let &ldquo;stored in India&rdquo; stand as a claim that is
            three-quarters true, and we will move these objects to India when that
            becomes possible.
          </p>
        </Note>
        <p>
          Beyond our own footprint, running an agent generally means an
          international transfer, because the providers you connect are wherever
          you have chosen to run them. For transfers to our processors we rely on
          the standard contractual clauses in their terms; for the providers you
          connect yourself, it is your own agreement with them that carries the
          transfer.
        </p>
      </>
    ),
  },

  {
    id: "retention",
    heading: "How long we keep it",
    body: (
      <>
        <p>
          <strong>By default we keep call content indefinitely.</strong> A
          workspace can set a retention policy — any number of days between 1 and
          3650 — and when a call reaches that age its content is deleted while the
          record of the call survives, because an invoice has to keep resolving.
          Only an admin can set it. Each call&rsquo;s deadline is fixed when the
          call ends, so changing the policy later does not move a deadline that
          already exists, in either direction.
        </p>
        <p>
          The same erasure runs immediately when a customer deletes a call
          themselves. It is not an audio feature: a transcript is personal data in
          exactly the way the recording is, and it is the copy that travels into
          analysis, webhooks and the model provider.
        </p>
        <DataTable
          columns={["An erased call", "What happens"]}
          rows={[
            [
              "Deleted",
              "The recording and the screen video; the whole transcript, in both directions; every tool call and its result; the images attached to it; the captured values, the summary, the outcome rationale and the extracted fields; the runtime trace; and the phone numbers on the call",
            ],
            [
              "Kept",
              "The call record itself — duration, status, outcome, how it ended and every cost column — the usage counters behind those costs, and a stamp saying the content was erased. Also one event, timestamp only, if a caller withdrew consent to being recorded: it is the evidence the content was handled correctly, which is exactly what someone asks for after the content is gone",
            ],
          ]}
        />
        <p>
          Customers can delete a call and its content, or just its audio, from the
          dashboard or the API, and can delete agents, tools, FAQs, secrets, provider
          keys, integrations and webhooks at any time. A text conversation has no
          delete of its own today; its content goes when the sessions inside it
          are erased, and the contact record goes with the last conversation.
        </p>
        <DataTable
          columns={["Everything else", "Retention"]}
          rows={[
            [
              "Account, workspace and membership records",
              "Until you ask us to delete them",
            ],
            ["FAQ content", "Until you delete the FAQ"],
            [
              "Stored credentials and OAuth tokens",
              "Until you remove the connection",
            ],
            [
              "Billing, credit and payment records",
              "As long as tax and accounting law requires",
            ],
            ["Server logs and operational metrics", "Short-lived, rotated"],
          ]}
        />
        <Note heading="There is no self-serve account deletion yet">
          <p>
            Leaving a workspace, or being removed from one, does not delete
            anything it holds. To have a workspace and everything in it erased,
            write to {SUPPORT_EMAIL} from the address on the account and we will do
            it. We would rather tell you that than put &ldquo;delete your
            account&rdquo; in a policy and leave you looking for the button.
          </p>
          <p>
            Backups age out on their own cycle and are not selectively edited.
          </p>
        </Note>
      </>
    ),
  },

  {
    id: "security",
    heading: "How it is protected",
    body: (
      <>
        <Bullets
          items={[
            "Traffic between your browser, our API and our infrastructure is encrypted with TLS.",
            "Provider keys, OAuth tokens and stored secrets are encrypted at rest with a key held outside the database, and are write-only over the API — no endpoint, model or support path returns one.",
            "Recordings and attachments sit in private buckets. Every link to one is signed, expires within an hour, and is scoped to a single object.",
            "Every workspace's data carries its identifier and every query is scoped to the workspace making it. Storage operations additionally refuse to touch an object outside the workspace's own prefix.",
            "Databases, queues and caches listen on no public interface. The signing key that mints sessions and tokens exists only on the control plane, so no region can forge another region's credentials.",
            "Requests that agents make outward — HTTP tools, code tools, webhooks — are blocked from reaching private, loopback and internal network addresses.",
            <>
              One honest exception: the leg of a phone call that crosses the public
              telephone network is carried by the carrier in the clear, as PSTN
              telephony everywhere is. It is not encrypted end to end and we do not
              claim it is.
            </>,
          ]}
        />
        <Note heading="We hold no security certification">
          <p>
            Talqing has not completed a SOC 2 audit, an ISO 27001 certification or
            any equivalent programme, and nothing on this page should be read as
            implying one. What is described above is what the software does, so you
            can weigh it against your own obligations. If you need something in
            writing for a procurement or legal review, ask us at {SUPPORT_EMAIL}.
          </p>
          <p>
            No system is perfectly secure. If we discover a breach affecting your
            data we will tell you and the relevant regulator as the law requires.
          </p>
        </Note>
      </>
    ),
  },

  {
    id: "recording",
    heading: "Recording, and the people your agents call",
    body: (
      <>
        <p>
          This is the section most likely to matter to you, whichever side of an
          agent you are on.
        </p>
        <Bullets
          items={[
            <>
              <strong>Voice and video agents record by default.</strong> One
              setting on the agent turns it off. Recording follows whichever agent
              is speaking, so an agent with it off records silence for as long as
              it holds the call.
            </>,
            <>
              <strong>Whether callers are told is a separate setting, and it is
              off by default.</strong> Turned on, the agent must speak a notice in
              its greeting — checked when the agent is published, so it cannot be
              switched on and then quietly dropped.
            </>,
            <>
              <strong>Recording can always be stopped.</strong> Whenever a call is
              being recorded, the agent carries a tool that stops it, and is
              instructed to reach for that tool as soon as a caller objects or asks
              for the recording to be deleted. Stopping discards the whole file,
              not merely what would have followed. That right does not depend on
              anyone having announced anything first.
            </>,
            <>
              <strong>Screen recording is off by default</strong> and only applies
              where a caller has chosen to share their screen with the agent.
            </>,
          ]}
        />
        <Note heading="Talqing does not decide whether recording is lawful where your callers are">
          <p>
            Recording law differs by jurisdiction and by who is on the call —
            all-party consent rules, the DPDP Act, the GDPR, TRAI&rsquo;s
            regulations. If you operate an agent, determining what applies to your
            calls and configuring the agent accordingly is yours to do. The default
            is on; that is a default, not advice.
          </p>
        </Note>
      </>
    ),
  },

  {
    id: "rights",
    heading: "Your rights",
    body: (
      <>
        <p>
          <strong>If you hold a Talqing account.</strong> Depending on where you
          live you may have the right to access your data, correct it, delete it,
          export it, object to processing, or withdraw consent. Most of these you
          can exercise yourself: agents, tools, FAQs, connections, calls
          and recordings are all deletable in the dashboard, and everything the
          dashboard shows you is readable over the API in a portable form. For
          anything else — including deleting the account itself — write to{" "}
          {SUPPORT_EMAIL}. We will respond within 30 days.
        </p>
        <p>
          <strong>If you spoke to an agent someone built here.</strong> Your rights
          are against that business, not against us; they control what was
          collected and they can delete it. Contact them first. If you cannot
          reach them, write to {SUPPORT_EMAIL} and we will pass the request on and
          tell you that we have.
        </p>
        <p>
          If you are unhappy with how a request was handled you may complain to
          your data protection authority. In India, that is the Data Protection
          Board.
        </p>
      </>
    ),
  },

  {
    id: "cookies",
    heading: "Cookies and browser storage",
    body: (
      <>
        <p>
          We set exactly one cookie, <code className="font-mono text-[0.9em] text-foreground">talqing_session</code>.
          It keeps you signed in after you authenticate with Google. It is
          HTTP-only, sent only over HTTPS, scoped to talqing.com so it reaches each
          region&rsquo;s API, and it lasts 30 days. It is strictly necessary, it is
          not used for tracking, and there is no analytics, advertising or
          third-party cookie anywhere on this site. That is why there is no cookie
          banner.
        </p>
        <p>
          The dashboard also keeps a little state in your browser&rsquo;s local
          storage — which region you last used, which workspaces you have seen,
          and the width of a panel you dragged. It never leaves your browser.
        </p>
        <p>
          Two things reach out from the page itself. Fonts are served from our own
          domain rather than from a font CDN. And before you sign in, the home
          and login pages ask our DNS provider&rsquo;s edge which country the
          request came from, so they can quote prices in your currency and offer
          you the nearer region — that is one request per page, carrying your IP
          address, and it sets nothing.
        </p>
      </>
    ),
  },

  {
    id: "google",
    heading: "Google user data",
    body: (
      <>
        <p>
          Talqing&rsquo;s use of information received from Google APIs adheres to
          the{" "}
          <a
            className="underline underline-offset-4 hover:text-foreground"
            href="https://developers.google.com/terms/api-services-user-data-policy"
          >
            Google API Services User Data Policy
          </a>
          , including the Limited Use requirements. We touch Google in two places
          and nowhere else.
        </p>
        <p>
          <strong>Sign-in.</strong> We request <code className="font-mono text-[0.9em] text-foreground">openid</code>,{" "}
          <code className="font-mono text-[0.9em] text-foreground">email</code> and{" "}
          <code className="font-mono text-[0.9em] text-foreground">profile</code>, and use them only to
          identify you and to show your name and picture to the other members of
          your workspace.
        </p>
        <p>
          <strong>Google Calendar</strong>, and only if you connect it. We request
          the narrowest scopes that let an agent do scheduling work: your list of
          calendars and their events, read-only; free/busy information; and write
          access limited to events on calendars you own — deliberately not the
          broader scope that would reach every calendar shared with you. We request
          no Gmail, Drive or Contacts scope, and there is no way to add one from
          the dashboard.
        </p>
        <p>
          Calendar data reached this way is used only to run the tools your agent
          calls. It is not transferred to anyone except as needed to provide that
          feature, it is never used for advertising, and no human at Talqing reads
          it — except with your explicit permission, where it is necessary for
          security, or where the law requires it. Disconnecting the integration in
          the dashboard revokes the token; you can also revoke it yourself at{" "}
          <a
            className="underline underline-offset-4 hover:text-foreground"
            href="https://myaccount.google.com/permissions"
          >
            myaccount.google.com/permissions
          </a>
          .
        </p>
      </>
    ),
  },

  {
    id: "children",
    heading: "Children",
    body: (
      <p>
        Talqing is a business product and is not directed at children. We do not
        knowingly collect data from anyone under 18. If you believe a child has
        given us data, write to {SUPPORT_EMAIL} and we will delete it.
      </p>
    ),
  },

  {
    id: "changes",
    heading: "Changes to this policy",
    body: (
      <p>
        We update this page when what we do changes, and revise the date at the
        top. If a change materially affects how we handle your data, we will tell
        account holders by email to the address on the account, or in the
        dashboard, before it takes effect.
      </p>
    ),
  },

  {
    id: "contact",
    heading: "Contact",
    body: (
      <>
        <p>
          For anything in this policy — a question, an access or deletion request,
          or a complaint — write to {SUPPORT_EMAIL}. Under India&rsquo;s data
          protection rules that address also reaches our grievance officer, who
          will acknowledge a grievance within the period the rules require.
        </p>
        <p>
          {LEGAL_ENTITY.name}
          <br />
          {LEGAL_ENTITY.address}
          <br />
          CIN {LEGAL_ENTITY.cin}
          <br />
          {SUPPORT_EMAIL}
          <br />
          {LEGAL_ENTITY.phone}
        </p>
        <p>
          The engineering detail behind sections 6 and 8 — exactly what an erasure
          deletes, and how the recording settings behave — is documented at{" "}
          <a
            className="underline underline-offset-4 hover:text-foreground"
            href={`${DOCS}/platform/data-retention-and-privacy`}
          >
            docs.talqing.com
          </a>
          .
        </p>
      </>
    ),
  },
];

export default function Privacy() {
  return (
    <LegalPage
      title="Privacy policy"
      summary="What we collect, who else sees it, where it is stored, and how long we keep it. Written against what the platform actually does — including the parts where the honest answer is not the flattering one."
      updated={UPDATED}
      sections={sections}
    />
  );
}
