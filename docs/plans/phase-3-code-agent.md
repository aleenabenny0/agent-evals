# Phase 3: the AWS code agent

**Summary.** A second agent, one that acts rather than only reads. Given an AWS task in plain
English, it writes boto3 code. Everything about it is graded by code. Every API call is checked
against botocore's service models, AWS's own definitions of each operation and its parameters. The
code then runs against moto, an in-memory fake of AWS, inside a Docker container with no network,
and hidden tests check the final state. Nothing runs against a real AWS account. The harness, the
results store, the statistics and the gate stay as they are. Estimated cost about $2, capped at $5.
Most of the cost is human time spent verifying the tasks.

**Status:** proposed, 2026-11-03. Nothing is built.

**Goal.** Show that the method carries to a different kind of agent. Here hallucination means
calling an operation or a parameter that does not exist, and being right means the account ends up
in the state the task asked for.

## A trade-off, stated first

`docs/plans/brief-alignment.md` notes that the briefs reward depth on one agent over a second agent.
The red team, prompt injection planted in pages the docs agent reads, adds depth to the agent that
already exists, and it is smaller. This plan follows the order chosen on 2026-11-03, and decision 1
confirms it.

## Part 1: a task and the agent

- **A case** is a task ("Create an SQS queue named `orders` whose messages stay hidden for 60
  seconds after a consumer reads them"), a setup that creates any resources that already exist,
  a hidden test that checks the final state, and a reference solution.
- **The agent** writes one function, `solve(session)`, that uses `session.client(...)`. Its tools:
  - `list_operations(service)` and `describe_operation(service, operation)`, read from the
    botocore service models installed with the project. They are local, free and exact.
  - `run_code(code)`, which runs the code in the sandbox against a fresh moto with the case's setup
    applied, and returns what it printed and any exception. The hidden test never runs here.
  - `submit_solution(code)`, checked against the output contract (ADR 0011) like `submit_answer`.
- **A closed-book baseline:** the same model with no tools and one submission, like the docs
  agent's. The difference between the two is what looking things up and trying them is worth.
- **Model:** Haiku 5.5, behind `LLMClient`, as for the docs agent.

## Part 2: the graders, all code (agree first)

- **API checks.** The submitted code is parsed, never imported. Each call on a client made with
  `session.client("svc")` is matched to a botocore operation by its method name, and its keyword
  arguments to the operation's input shape. Metrics:
  - `hallucinated_operation`: a method that names no operation of that service.
  - `hallucinated_parameter`: a keyword the operation does not take.
  - `missing_required_parameter`.
  - `unchecked_calls`: calls the parser cannot resolve, such as `getattr` or `**kwargs`. These are
    counted and reported, never silently passed.
- **`task_passed`.** In the sandbox: a fresh moto, the setup, the agent's `solve`, then the hidden
  test. It passes if nothing raises and every assertion holds.
- **`unrequested_destructive_call`.** A `Delete*`, `Terminate*` or `Remove*` operation the task did
  not ask for.
- **Impossible tasks.** Some cases ask for a setting that does not exist. The right outcome is an
  abstention that says so. Inventing a parameter scores as a hallucination.
- The existing metrics carry over: errors, the output contract, model calls, cost and latency, each
  with its interval, compared paired.

## Part 3: the sandbox

- A Docker container per run, with `--network none`, a read-only filesystem except a temporary
  directory, limits on CPU and memory, a time limit per run, a non-root user, and fake AWS
  credentials. moto's `mock_aws` is active inside it. Model-written code never runs on the host.
- Offline tests prove the walls hold: a request to a public address fails, a file on the host
  cannot be read, an endless loop is stopped, and the real AWS endpoints cannot be reached.
- Tests that need Docker are marked, and are skipped where Docker is absent, so the rest of the
  suite stays fast and offline.

## Part 4: the cases

- **Services** that moto supports well: S3, DynamoDB, SQS, SNS, IAM, KMS, Secrets Manager, Systems
  Manager parameters and EC2 networking.
- **Types:** create a resource with settings, modify one that exists, query state and return a
  value, multi-step (two services wired together, such as a topic delivering to a queue), and
  impossible.
- **120 cases, 60 dev and 60 test,** split once with a fixed seed. Drafts are written by an agent
  into `suites/code/*.drafts.jsonl` with `provenance: synthetic`, as AGENTS.md requires.
- **Checks by machine before a person sees a case:** the reference solution passes the hidden test
  on moto, a solution that does nothing fails it, and the reference with its key setting removed
  fails it too. So each test is known to detect the thing the task asks for.
- **Then the author verifies each one:** does the test check what the task says, no more and no
  less? With the machine checks done, this is about 2 minutes a case, or 4 hours in all.
- **Power.** 60 paired cases detect only large differences: 11 to 16 points when 10% to 20% of
  cases disagree between two versions (`stats/power.py`, 80% power). With 40, it is 14 to 20
  points. Every result states this beside it.

## Build order, and what each part needs

| Part | Needs | Paid |
|---|---|---|
| 1. Sandbox and its tests | Docker Desktop | no |
| 2. API checker and the other graders, with tests | this plan agreed | no |
| 3. Case model, drafts and machine checks | no | no |
| 4. The author verifies the cases | about 4 hours | no |
| 5. The agent, tested offline with a scripted model | no | no |
| 6. A smoke run of 10 cases | approval | about $0.05 |
| 7. Dev, 3 attempts, and the closed-book baseline | approval | about $1.20 |
| 8. Test, once | approval | about $0.40 |

Each part is committed on its own, so stopping after any part leaves working software.

## Cost

A code task should take more model calls than a docs question, about 6 to 8, at about $0.004 to
$0.006 per attempt at Haiku's prices. Dev (180 attempts), the baseline (60), the test run (60) and a
smoke run come to about $2. The cap is $5.

## Risks

- **moto is not AWS.** It does not implement every operation, and it checks less than AWS does, so
  code can pass on moto and fail on AWS. Cases stay within operations moto supports well, and every
  reference solution is checked on it. Results describe moto, and say so.
- **The parser sees only what it can resolve.** Calls it cannot resolve are counted, not guessed.
- **API versions.** The checker uses the botocore models pinned in `uv.lock`. An operation newer
  than that pin would read as hallucinated, so the pin is recorded in each run's manifest.
- **Scope.** This is the largest build after phase 8.

## Decisions

1. **Order.** (a) Build the code agent now, as chosen. *Recommended.* (b) Do the red team first,
   for depth on the docs agent.
2. **The agent's tools.** (a) API lookup and runs in the sandbox, with the closed-book baseline
   beside it. *Recommended.* (b) API lookup only. (c) Runs only.
3. **Cases.** (a) 120 cases, 60 and 60, about 4 hours of verification. *Recommended.* (b) 80 cases,
   40 and 40, about 3 hours, and coarser results.
4. **Sandbox.** (a) Docker with no network, as above. *Recommended.* (b) A plain subprocess on the
   host: faster, but model-written code would run on this machine.
5. **Budget.** (a) A ledger capped at $5 for the paid runs. *Recommended.* (b) Another cap.
