import type { Metadata } from "next";

import { Bullets, DataTable, LegalPage, Note, type LegalSection } from "../legal-page";
import { LEGAL_ENTITY, SUPPORT_EMAIL } from "../marketing-chrome";

export const metadata: Metadata = {
  title: "Terms of service — Talqing",
  description:
    "The agreement that governs your use of the Talqing platform, dashboard and API.",
  alternates: { canonical: "/terms" },
  robots: { index: true, follow: true },
};

const UPDATED = "2 October 2026";

const privacyLink = (
  <a className="underline underline-offset-4 hover:text-foreground" href="/privacy">
    privacy policy
  </a>
);

// The two clauses that carry the real risk in this document are §7 (calls,
// messages and recording) and §10 (fees). Both describe product behaviour that
// has changed under them before:
//
//  - Recording shipped ON BY DEFAULT with caller disclosure OFF by default.
//    §7 is where that default is handed to the customer as their obligation. If
//    the default ever flips, §7 and privacy §8 both change with it.
//  - Strict BYOK means the platform fee is the whole of what we charge. The
//    previous draft said provider cost was "passed through", which would have
//    described us as reselling model and carrier minutes we never touch.
const sections: LegalSection[] = [
  {
    id: "agreement",
    heading: "The agreement",
    body: (
      <>
        <p>
          These terms are a contract between you and {LEGAL_ENTITY.name} (CIN{" "}
          {LEGAL_ENTITY.cin}), {LEGAL_ENTITY.address} (&ldquo;Talqing&rdquo;).
          They govern the Talqing website, documentation, dashboard and API (the
          &ldquo;Service&rdquo;). By creating an account or using the Service you
          accept them. If you are accepting on behalf of a company, you confirm
          you are authorised to bind it, and &ldquo;you&rdquo; means that company.
        </p>
        <p>
          These terms and the {privacyLink} are the whole of the agreement. Where
          they disagree, these terms govern everything except the handling of
          personal data, where the privacy policy does.
        </p>
      </>
    ),
  },

  {
    id: "preview",
    heading: "The Service is in preview",
    body: (
      <p>
        Talqing is pre-release. Anyone with a Google account can sign up, and that
        is deliberate — but features change without notice, interfaces may break
        between releases, and there is no uptime commitment and no service level
        agreement. Do not put the Service on a path where an outage causes you
        loss you are not prepared to absorb.
      </p>
    ),
  },

  {
    id: "accounts",
    heading: "Accounts, workspaces and roles",
    body: (
      <>
        <p>
          Sign-in is Google OAuth; there is no password. Your work lives in a
          workspace, and a workspace can hold several members with one of three
          roles — admin, editor or viewer. Admins can invite and remove members,
          hold the workspace&rsquo;s credentials, spend its money and set its
          retention policy.
        </p>
        <p>
          You are responsible for everything done under your account, for the
          access you grant to members of your workspace, and for any personal
          access tokens you create — a token acts with its owner&rsquo;s
          permissions and does not expire until it is deleted. Tell us at{" "}
          {SUPPORT_EMAIL} as soon as you believe an account or token has been
          compromised.
        </p>
        <p>
          You must be at least 18 and not barred from receiving the Service under
          applicable law.
        </p>
      </>
    ),
  },

  {
    id: "your-content",
    heading: "Your content stays yours",
    body: (
      <p>
        Your agents, prompts, tools, transcripts, recordings and
        data remain yours. You grant us only the licence we need to operate the
        Service for you: to store your content, to process it, and to transmit it
        to the providers, carriers and endpoints your agent is configured to use.
        We do not use your content to train models. How we handle it is described
        in the {privacyLink}.
      </p>
    ),
  },

  {
    id: "byok",
    heading: "You bring your own keys",
    body: (
      <>
        <p>
          Talqing holds no model credentials of its own. Every language model,
          speech-to-text, text-to-speech and avatar your agents run is called with
          your key, on your account, and the provider bills you directly. The
          carrier that carries a phone call is your account too. We add a platform
          fee and nothing else; see section 10.
        </p>
        <p>
          Two consequences follow, and both are yours rather than ours. Your
          relationship with each provider is directly with them, governed by their
          terms, their pricing, their rate limits and their acceptable-use rules —
          an outage or a policy change on their side may degrade or stop your
          agents, and we are not responsible for their availability. And your
          spend with them is uncapped by us: the Service does not throttle or
          budget what your agents cost you at your providers, so a runaway agent
          or an oversized batch spends your money at their rates. Set your limits
          in their consoles.
        </p>
        <p>
          Removing a key from Talqing does not un-publish anything. Agents keep
          answering and fail the moment they reach that provider.
        </p>
      </>
    ),
  },

  {
    id: "acceptable-use",
    heading: "Acceptable use",
    body: (
      <>
        <p>
          The Service places phone calls and sends messages to real people, which
          makes this section the one that matters most. You must not use Talqing
          to:
        </p>
        <Bullets
          items={[
            "Place calls or send messages to anyone who has not consented to receive them, or who is on a do-not-call or do-not-disturb register that applies to them.",
            "Break telemarketing, telecoms, email or messaging law in any jurisdiction you operate in or contact into — including India's TRAI regulations, the US TCPA, and the platform policies of any channel you send through.",
            "Impersonate a person or organisation, or deny that the caller is an AI agent where the law or the platform requires that disclosure.",
            "Run fraud, phishing, or any scheme that extracts money, credentials or personal data by deception.",
            "Collect sensitive personal data — health, financial, biometric or government identifiers — without the legal basis to do so.",
            "Harass, threaten, or send material that is unlawful, defamatory or hateful.",
            "Enrich a person's contact details without the right to use them for the purpose you are using them for.",
            "Point a tool, a code operation or a webhook at a system you are not authorised to reach, or use the Service to probe or attack anyone — including us.",
            "Attack the Service, circumvent its limits, or resell access to it. Talqing's source code is separately available under its own licence, which governs self-hosting and which these terms do not change.",
          ]}
        />
        <p>
          We may suspend an account immediately, without notice, where we believe
          this section is being breached. There is no automated rate limit or
          spend cap standing between an agent and the outside world today, which
          is precisely why this section is a contractual obligation rather than a
          setting.
        </p>
      </>
    ),
  },

  {
    id: "calls",
    heading: "Calls, messages and recording",
    body: (
      <>
        <p>
          <strong>You are the caller, not us.</strong> You choose who your agents
          contact and what they say. Obtaining consent, honouring opt-outs,
          disclosing that the caller is automated, and complying with the rules of
          every jurisdiction you contact into are your obligations.
        </p>
        <Note heading="Recording is on by default, and caller disclosure is not">
          <p>
            Every voice and video agent you publish records its calls unless you
            turn recording off, and it says nothing about that to the caller unless
            you turn the disclosure setting on. Both settings are per agent and
            both are yours to set.
          </p>
          <p>
            Recording law differs by jurisdiction and by who is on the call —
            all-party consent rules, the DPDP Act, the GDPR, sectoral rules where
            you operate. <strong>Talqing does not determine what applies to your
            calls and does not warrant that any default is lawful for you.</strong>{" "}
            Before you publish an agent, decide what your callers must be told and
            configure it. Where an agent is recording, it carries a tool that stops
            and discards the recording when a caller objects, and it is instructed
            to use it.
          </p>
        </Note>
        <p>
          The same principle covers screen sharing, images a caller sends, and any
          transfer of a call to a person: what your agent collects is what you have
          decided to collect.
        </p>
      </>
    ),
  },

  {
    id: "end-users",
    heading: "The people your agents talk to",
    body: (
      <>
        <p>
          For the personal data of the people your agents speak to, you are the
          controller and we are your processor. You must have a lawful basis for
          the processing, publish your own privacy notice, and handle the requests
          those people make about their data.
        </p>
        <p>Acting as your processor, we undertake that:</p>
        <Bullets
          items={[
            "We process that data only to provide the Service to you and on your documented instructions, which your configuration of the Service is.",
            "We engage the sub-processors listed in the privacy policy, and will give notice before adding one. Providers you connect yourself are your own processors, not ours — we transmit to them because you told us to.",
            "The people who can access it are bound to confidentiality and are only those who need it to run or support the Service.",
            "We keep the security measures described in the privacy policy, and will tell you without undue delay if we become aware of a breach affecting your data.",
            "We will help you, so far as we reasonably can, with your own obligations — responding to the people your agents spoke to, and with impact assessments where one applies.",
            "We delete data on your instruction. Your retention policy, the delete endpoints and a written request to us are all such instructions.",
          ]}
        />
        <p>
          If your regulator requires these terms in a separate signed document,
          write to {SUPPORT_EMAIL}.
        </p>
      </>
    ),
  },

  {
    id: "third-party",
    heading: "Third-party services",
    body: (
      <p>
        Beyond the model and carrier accounts covered in section 5, the Service
        connects to apps you authorise, to any MCP server or HTTP endpoint you
        name, and to webhook destinations you configure. Each of those
        relationships is between you and them. We are not responsible for their
        availability, their content, or what they do with what your agent sends
        them, and we do not review the endpoints you point an agent at.
      </p>
    ),
  },

  {
    id: "fees",
    heading: "Fees, credits and payment",
    body: (
      <>
        <p>
          Talqing charges a flat platform fee and nothing else. Provider
          and carrier costs are billed to you by them, at your rates, and never
          pass through us — there is no markup and no resale of model tokens or
          telephony minutes.
        </p>
        <DataTable
          columns={["Channel", "Platform fee"]}
          rows={[
            ["Voice", "$0.0035 per minute — phone calls and browser calls alike"],
            ["Video", "$0.01 per minute, while an avatar is on the call"],
            ["Text", "$0.0001 per message the agent answers"],
          ]}
        />
        <p>
          Minutes are the call&rsquo;s own duration, metered to the second. A
          message is charged once the agent has answered it; one that failed, or
          was superseded by a newer message before it was answered, is not. There
          is no minimum, no connection fee and no subscription. A call that failed
          to run — refused by the carrier, never answered, or broken by an error
          on our side — is not charged the fee. A call that connected and went
          badly is a completed call, and is.
        </p>
        <p>
          <strong>The fee is drawn from a prepaid credit balance</strong>, bought
          in fixed packs through our payment processor&rsquo;s hosted checkout —
          your card details never reach us. Only an admin can buy. Credits are
          denominated in US dollars and are held per region: a balance in one
          region does not pay for a call in another and cannot be transferred
          between them. When a workspace reaches zero, new calls are refused and
          calls already running are never interrupted.
        </p>
        <p>
          Credit is prepaid and, once applied, is non-refundable and has no cash
          value, except where the law requires otherwise or where a payment is
          reversed by the processor. Amounts are exclusive of taxes and any
          currency-conversion or processing charges, which fall to you. We may
          change the platform fee on 30 days&rsquo; notice to the email on your
          account; credit already bought is unaffected.
        </p>
        <p>
          Promotional credit — including the credit granted when you first sign in
          — is a courtesy, not a purchase. We may vary or withdraw it for the
          future at any time.
        </p>
      </>
    ),
  },

  {
    id: "ip",
    heading: "Our intellectual property",
    body: (
      <p>
        The Service, its software, its interfaces and the Talqing name and marks
        are ours. Nothing here transfers them to you. Feedback you send us we may
        use freely and without obligation.
      </p>
    ),
  },

  {
    id: "confidentiality",
    heading: "Confidentiality",
    body: (
      <p>
        Each of us may learn non-public information about the other. Neither will
        disclose it or use it outside this agreement, except where the law compels
        disclosure — and then only after telling the other, if we are permitted
        to.
      </p>
    ),
  },

  {
    id: "disclaimer",
    heading: "Disclaimers",
    body: (
      <>
        <p>
          The Service is provided &ldquo;as is&rdquo; and &ldquo;as
          available&rdquo;. To the fullest extent the law allows, we disclaim all
          warranties, express or implied, including merchantability, fitness for a
          particular purpose and non-infringement.
        </p>
        <p>
          AI agents produce probabilistic output. They can be wrong, they can be
          confidently wrong, and they can be induced by a determined user to say
          things you did not intend. Do not deploy an agent on a task where a wrong
          answer causes harm without a human check in the path. You are responsible
          for reviewing and testing your agents before you publish them, and for
          what they say once you have.
        </p>
      </>
    ),
  },

  {
    id: "liability",
    heading: "Limitation of liability",
    body: (
      <>
        <p>
          To the fullest extent the law allows, neither party is liable for
          indirect, incidental, special, consequential or punitive damages, or for
          lost profits, revenue, data or goodwill. Our total liability arising out
          of or relating to this agreement is limited to the greater of the amount
          you paid us in the three months before the claim, or one hundred US
          dollars.
        </p>
        <p>
          For the avoidance of doubt, what you spend with your own model providers
          and carriers is not an amount paid to us and does not raise that cap.
        </p>
        <p>Nothing here excludes liability that cannot be excluded by law.</p>
      </>
    ),
  },

  {
    id: "indemnity",
    heading: "Indemnity",
    body: (
      <p>
        You will defend and indemnify us against claims, damages and costs arising
        from your use of the Service, your content, the calls, messages and
        recordings your agents make, and your breach of these terms — in
        particular, claims brought by the people your agents contact.
      </p>
    ),
  },

  {
    id: "termination",
    heading: "Suspension and termination",
    body: (
      <>
        <p>
          You may stop using the Service at any time. There is no self-serve
          account deletion today: to have your workspace and everything in it
          erased, write to {SUPPORT_EMAIL} from the address on the account. Leaving
          a workspace, or being removed from one, deletes nothing it holds.
        </p>
        <p>
          We may suspend or terminate access if you breach these terms, if your use
          threatens the Service or another user, or if the law requires it. We may
          also discontinue the Service — while it is in preview, on reasonable
          notice. On termination your right to use the Service ends. Export
          anything you want to keep first; unused credit is not refunded on
          termination for breach.
        </p>
      </>
    ),
  },

  {
    id: "changes",
    heading: "Changes to these terms",
    body: (
      <p>
        We may update these terms and will revise the date at the top. Material
        changes will be notified to account holders by email to the address on the
        account, or in the dashboard, before they take effect. Continuing to use
        the Service after that is acceptance.
      </p>
    ),
  },

  {
    id: "law",
    heading: "Governing law",
    body: (
      <p>
        These terms are governed by the laws of {LEGAL_ENTITY.jurisdiction},
        without regard to conflict-of-law rules. The courts of{" "}
        {LEGAL_ENTITY.courts} have exclusive jurisdiction over any dispute.
      </p>
    ),
  },

  {
    id: "general",
    heading: "General",
    body: (
      <>
        <p>
          If a provision is unenforceable the rest survives. Not enforcing a right
          is not waiving it. You may not assign this agreement without our consent;
          we may assign it in a merger or sale of the business. Neither party is
          liable for a failure caused by something outside its reasonable control.
        </p>
        <p>Questions: {SUPPORT_EMAIL}</p>
      </>
    ),
  },
];

export default function Terms() {
  return (
    <LegalPage
      title="Terms of service"
      summary="The agreement between you and Talqing. It covers what you may build, whose keys and whose money run it, who is responsible for the calls your agents make, and what happens when something goes wrong."
      updated={UPDATED}
      sections={sections}
    />
  );
}
