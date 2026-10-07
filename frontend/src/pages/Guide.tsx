// Guide — how to actually use StorageLens, written for a first-time
// operator. Every step names the control in this app that performs it.
// Deploy commands live in the README (Getting started); this page
// covers the day-to-day flow inside the product. No numbers invented
// here: the estimate/pricing caveats the rest of the app carries apply
// verbatim.

import { Link } from "react-router-dom";
import {
  Plug,
  KeyRound,
  Upload,
  History,
  Zap,
  ShieldCheck,
  Receipt,
  BellRing,
  FlaskConical,
} from "lucide-react";
import { PRICING_CHECKED, PRICING_REGION } from "../utils/pricing";
import { PageHeader } from "../components/ui/atoms";

// A shell command the operator is expected to run outside the console.
function Cmd({ children }: { children: string }) {
  return (
    <pre
      style={{
        background: "var(--code-bg, rgba(127,127,127,0.12))",
        border: "1px solid var(--border)",
        borderRadius: 8,
        padding: "8px 10px",
        fontSize: 11,
        lineHeight: 1.55,
        whiteSpace: "pre-wrap",
        overflowWrap: "anywhere",
        margin: "6px 0 0",
      }}
    >
      {children}
    </pre>
  );
}

const STEPS: {
  step: string;
  title: string;
  icon: React.ReactNode;
  body: React.ReactNode;
}[] = [
  {
    step: "1",
    title: "Deploy the stack, then connect",
    icon: <Plug size={15} strokeWidth={1.8} />,
    body: (
      <>
        Prerequisites: an AWS account you own, AWS credentials configured,{" "}
        <strong>SAM CLI</strong>, Python 3.13 and Node 20. From the repository
        root:
        <Cmd>{`cd infrastructure && sam build && cd ..
python scripts/check_deployment_permissions.py
sam deploy --template-file infrastructure/.aws-sam/build/template.yaml \\
  --stack-name intelligent-storage-cost-optimizer \\
  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM --resolve-s3
python scripts/deploy_and_configure.py`}</Cmd>
        The preflight script tells you up front whether your AWS identity can
        deploy. The last command reads the stack's outputs and{" "}
        <strong>writes <code>frontend/.env</code> for you</strong> — then set{" "}
        <code>VITE_API_BASE_URL</code> to your API base URL (never commit it —
        see <Link to="/settings">Settings</Link>). The chip in the top bar
        turns <strong>Connected</strong> green once the API answers. Without
        it the console runs in offline mode: experiment pages still work from
        their bundled artifacts, but the live inventory is empty.
      </>
    ),
  },
  {
    step: "2",
    title: "Sign in with a Cognito user",
    icon: <KeyRound size={15} strokeWidth={1.8} />,
    body: (
      <>
        The pool does not offer public sign-up — identity is managed on
        purpose, so no anonymous account can reach your API. Whichever user
        created the stack mints accounts with:
        <Cmd>{`aws cognito-idp admin-create-user --user-pool-id <UserPoolId> \\
  --username alice@yourcompany.com \\
  --user-attributes Name=email,Value=alice@yourcompany.com Name=email_verified,Value=true
aws cognito-idp admin-add-user-to-group --user-pool-id <UserPoolId> \\
  --username alice@yourcompany.com --group-name Approvers`}</Cmd>
        <strong>Users</strong> can browse and read everything;{" "}
        <strong>Approvers</strong> can additionally approve, execute,
        bulk-propose and manage on-call shifts. Try an action you're not
        allowed and the API says so instead of hiding the control.
      </>
    ),
  },
  {
    step: "3",
    title: "Get documents into the system",
    icon: <Upload size={15} strokeWidth={1.8} />,
    body: (
      <>
        Upload individual files from <Link to="/documents">Documents →
        Upload</Link> — the API issues a presigned S3 upload and registers the
        document. To mirror an existing S3 bucket, run the{" "}
        <Link to="/onboarding">Onboarding wizard</Link>, which pre-checks the
        import before anything moves. For whole corpora the bulk loaders work
        headlessly — for the synthetic demo seed, from the repository root:
        <Cmd>{`python scripts/load_synthetic_data.py \\
  --stack-name intelligent-storage-cost-optimizer \\
  --region ap-south-1 --purge-access-history`}</Cmd>
        ; use <code>scripts/load_real_data.py</code> for the GovInfo corpus
        instead). Each document records its size, content type, storage tier
        and lifecycle <strong>state</strong> (Active / Closed / Archived — the{" "}
        <Link to="/documents">Documents</Link> page legend explains what each
        state allows the optimizer to recommend).
      </>
    ),
  },
  {
    step: "4",
    title: "Fold downloads into access history",
    icon: <History size={15} strokeWidth={1.8} />,
    body: (
      <>
        When a file is downloaded, that access event is recorded. Dashboard →
        "Aggregate 30-day access history" → <strong>Run Analysis</strong> folds
        recent events into per-document access features and writes the
        aggregates-table snapshot the optimizer consumes (the backend also runs
        it on a daily schedule). Run this after new activity so the next
        recommendation sees it.
      </>
    ),
  },
  {
    step: "5",
    title: "Propose decisions",
    icon: <Zap size={15} strokeWidth={1.8} />,
    body: (
      <>
        <Link to="/approvals">Approvals → Propose decisions</Link> re-runs the
        engine over the live aggregates and queues one proposal per document it
        would migrate (up to 25 per click; repeat for more). Each proposal
        names the move — current tier → recommended tier, predicted annual
        saving, and the forecast access band. Documents the engine skips are
        listed with their reason (already pending, stale aggregate, or
        archived in flexible/deep tiers where S3 demands a restore first).
      </>
    ),
  },
  {
    step: "6",
    title: "Approve, execute, verify",
    icon: <ShieldCheck size={15} strokeWidth={1.8} />,
    body: (
      <>
        This is the loop the product runs <em>for</em> you — you never hand-run{" "}
        <code>s3 cp</code>. In the queue, <strong>Approve</strong> a proposal
        you accept, then <strong>Execute</strong> (two clicks; it's
        irreversible): StorageLens copies the object onto itself in S3 under
        the approved class, HEAD-verifies the result, and only when S3
        confirms it records the realized saving. If the copy fails, the
        decision is marked <strong>Failed</strong> with the exact reason and
        nothing is counted — see the History tab for both outcomes.
      </>
    ),
  },
  {
    step: "7",
    title: "Read the ledger and reconcile",
    icon: <Receipt size={15} strokeWidth={1.8} />,
    body: (
      <>
        <Link to="/approvals">Approvals → Ledger</Link> lists every verified
        transition with its realized saving and uncertainty band — measured
        outcomes, not forecasts. <strong>Reconciliation</strong> then compares
        that ledger against AWS Cost Explorer for the same period, so you can
        see which tracked savings actually show up on the bill (an S3 line
        item takes a day or so to surface there).
      </>
    ),
  },
  {
    step: "8",
    title: "Keep the lights on (on-call)",
    icon: <BellRing size={15} strokeWidth={1.8} />,
    body: (
      <>
        <Link to="/oncall">On-Call</Link> schedules the engineers behind the
        stack: admins create shifts, the page shows who's covering right now,
        and when CloudWatch alarms fire the on-call gets paged (Optionally via
        PagerDuty — fill the deployed secret with your routing key and API
        token; without it every page is recorded as an audited skip instead).
        The notification history shows what fired, who was on, and where it
        went.
      </>
    ),
  },
  {
    step: "9",
    title: "Validate before you trust it",
    icon: <FlaskConical size={15} strokeWidth={1.8} />,
    body: (
      <>
        <Link to="/experiments">Experiments</Link> reruns the optimizer over
        three datasets — a synthetic law-firm workload, a real GovInfo mix, and
        a year of U.S. District Court opinions — and compares it against an
        age-based baseline policy (compare with <Link to="/policies">Policies</Link>,
        which models governance rules like minimum-savings thresholds and
        protected collections). Everything there is modeled and badged as
        experiment data.
      </>
    ),
  },
];

export default function Guide() {
  return (
    <div className="page-in">
      <PageHeader
        title="How to use StorageLens"
        subtitle="A five-minute operator tour — what the product does, and which control does it."
      />

      <section className="panel panel-pad" style={{ marginBottom: 18 }}>
        <p style={{ color: "var(--text-2)", fontSize: 13, lineHeight: 1.7 }}>
          <strong style={{ color: "var(--text)" }}>What this product is. </strong>
          StorageLens is storage-cost intelligence for S3-stored document
          collections: it watches what you actually download, models the next
          12 months of storage / retrieval / request / transition fees per
          document, and proposes the cheapest compliant tier. Approve it, and
          the product executes the S3 transition itself, verifies it, and
          records the realized saving in a ledger it reconciles against AWS
          Cost Explorer. It runs against an AWS serverless stack you deploy,
          so the data stays yours.
        </p>
        <p
          className="muted"
          style={{ fontSize: 12, lineHeight: 1.7, marginTop: 10 }}
        >
          Two honesty rules the whole console follows: every cost figure is an{" "}
          <strong>estimate</strong> built on the optimizer's pricing snapshot
          (region {PRICING_REGION}, snapshot {PRICING_CHECKED}, validated against
          the AWS Price List API — not an AWS invoice), and pages distinguish{" "}
          <strong>live system data</strong> from{" "}
          <strong>experiment data</strong> with badges, so a modeled number is
          never mistaken for a measured one.
        </p>
      </section>

      <section
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
          gap: 14,
        }}
      >
        {STEPS.map((s) => (
          <section
            key={s.step}
            className="panel panel-pad"
            style={{ display: "flex", flexDirection: "column", gap: 8 }}
          >
            <span className="row" style={{ gap: 9 }}>
              <span
                className="row"
                aria-hidden="true"
                style={{
                  width: 26,
                  height: 26,
                  flex: "0 0 26px",
                  borderRadius: 8,
                  alignItems: "center",
                  justifyContent: "center",
                  background: "var(--accent-dim)",
                  color: "var(--accent-strong)",
                  fontWeight: 700,
                  fontSize: 12.5,
                }}
              >
                {s.step}
              </span>
              <h2 style={{ fontSize: 14.5, letterSpacing: "-0.01em" }}>
                {s.title}
              </h2>
              <span className="muted" style={{ marginLeft: "auto" }}>
                {s.icon}
              </span>
            </span>
            <p style={{ color: "var(--text-2)", fontSize: 12.5, lineHeight: 1.65 }}>
              {s.body}
            </p>
          </section>
        ))}
      </section>

      <section className="panel panel-pad mt-3">
        <div className="panel-title">The 30-second version</div>
        <p style={{ color: "var(--text-2)", fontSize: 13, lineHeight: 1.7, marginTop: 8 }}>
          Deploy the stack and create your Cognito users (steps 1–2 above —
          the exact commands are on this page) → point the console at your API
          →
          ingest documents → run aggregation daily → approve → execute → the
          Ledger shows verified realized savings → Reconciliation checks them
          against Cost Explorer → schedule on-call so alarms page a human. The
          product works almost entirely off metadata and access logs, so
          pointing it at an existing bucket costs nothing but the entries you
          register; every S3 move it makes is one you approved first.
        </p>
      </section>
    </div>
  );
}