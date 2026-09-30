# PyPI Change Intelligence

PyPI Change Intelligence is a local release-triage service that turns selected PyPI
release events into evidence-backed findings for engineering teams.

## Prerequisites

- macOS, Linux, or WSL2 with Linux containers
- Docker with a responsive local daemon and Compose v2
- GNU Make and Python 3.12+
- Network access for the initial image build and the live PyPI phase

Remote Docker contexts and native Windows containers are not supported. Check the local
launch boundary without starting anything:

```sh
sh infra/doctor.sh launch
```

## Run the assessment demo

```sh
make up
```

Compose processes the banked RSS fixtures through the retained stack, verifies fresh
database results and two consecutive zero-lag samples, and then starts the monitored
live PyPI RSS pipeline. The live phase uses public PyPI evidence and defaults to the fake
model when no OpenAI API key is configured.

To enable the paid model-assisted path, copy the example environment and set the key:

```sh
cp .env.example .env
# In .env: OPENAI_API_KEY=<your key>
make up
```

Shell values override `.env`, so the equivalent paid one-command launch is:

```sh
OPENAI_API_KEY=... make up
```

A configured key automatically enables the paid live path. The banked fixture phase
always uses the fake model. Set `MODEL_MODE=fake` to disable paid calls while retaining
the key locally.

| Surface           | URL                          |
| ----------------- | ---------------------------- |
| Findings UI       | <http://localhost:3000>      |
| API documentation | <http://localhost:8000/docs> |
| Grafana           | <http://localhost:3001>      |
| Redpanda Console  | <http://localhost:8080>      |

`make up` starts the stack detached, reads `docker compose port grafana 3000`,
and recreates only the API with that browser URL. This overrides stale
`GRAFANA_BASE_URL` values and supports Docker-assigned ports. Use `docker compose logs -f`
to follow output. Direct `docker compose up` uses the configured URL without discovery.

For a custom project or Compose files, pass the same global options to the launcher:

```sh
python3 -m scripts.start_stack -- -p my-demo -f docker-compose.yml -f docker-compose.override.yml
```

To repair links in an already running stack, add `--sync-only` before `--`.
This recreates only the API and leaves Grafana and the pipeline running.

## Drain safely

```sh
docker compose stop --timeout 60 connect-source && \
  docker compose run --rm --no-deps drain-gate
```

This stops ingress first and then requires two consecutive zero-lag samples from the
reasoning, database sink, and trace bridge consumer groups. A failed check leaves ingress
stopped and the rest of the stack available for inspection.

After a successful drain, stop the stack while preserving retained volumes:

```sh
docker compose down --timeout 60 --remove-orphans
```

Do not add `--volumes` unless destroying local demo data is intentional.

## Verify the stack

```sh
make smoke
```

The smoke test uses an isolated fixture/fake project, verifies the full data and
telemetry path, and removes only its own resources. It does not activate live ingress or
modify the retained demo stack.

For fixture-only development:

```sh
cp .env.example .env
make up-fixture
```

## Tradeoffs

### One classification call vs multi-step reasoning loop

Based on the larger architecture decision of calling into reasoning as minimal as
possible, I allotted the multi reasoning steps after a lot of deterministic checks.
Every deterministic check is completed before any llm call is made. Using a multistep
pipeline allows us to mitigate the risk compared to a single call by reducing the scope
of reasoning each step has to make. The first step is a materiality assessment, which
provides a label on top of the deterministically retrieved data. Then goes into an
applicability assessment, which gets lower level into where the impact of this event is.
Finally transitioning from a detail specific output from step 2, into a more customer
facing summary in step 3 which is what is surfaced to customers, answering the ‘what
does an engineer need to know?’. By separating each of the three we’re able to conduct
analysis on each step and understand the strengths and weaknesses of each portion. A
single classification call could cause issues we would not face by breaking it into
several calls and allowing us to measure cost and performance modularly. I tried using a
single classification call at first, but the amount of straying away from a proper
feedback loop was troublesome. I decided to narrow down the context and each step was
narrower which allowed me to conduct tests for each piece instead of calling once. See
[OpenAI's orchestration guidance](https://developers.openai.com/tracks/building-agents#orchestration).

### How to bound llm latency/cost

A large portion of my troubleshooting was getting to a point of reducing the cost.
Finding the line between reducing the cost and impacting the product itself is
important. This led to creating an architecture that leans on verification and manually
building out as much context as possible before making any llm call. This meant, If we
do as much work as possible on our own end before making any reasoning call, we can
reduce not only the amount of time the reasoning call takes, but alongside that the cost
of reasoning. Every step throughout the entire product is able to complete the record,
whether that’s it being invalid, or gathering enough evidence deterministically to not
have to use reasoning. It’s important to lean into the low cost worker compute as much
as possible before handing things off to an LLM. So not only is there filtering with
connect, but also major patterns of records I found during testing are dealt with
deterministically. See [Redpanda Connect filtering and sampling documentation](https://docs.redpanda.com/connect/cookbooks/filtering/).
The minority is passed into the reasoning calls and can still be
rejected eventually too. This creates an example of using an LLM where required, not as
a lever to pull for basic compute power. LLM costs are subtle, but by architecting from
the ground up to mitigate it you can land on a product that is AI enhanced rather than
AI dependent. An important tool used during the latency/cost portion is the
observability stack. See [Redpanda Connect tracing output documentation](https://docs.redpanda.com/connect/components/tracers/redpanda/).
Being able to trace every call independently and understand what it
looked like was essential to coordinating prioritization. Gathering details to
understand the current performance of the application is important especially when
asking “how can I reduce the latency/cost” It’s not possible to reduce something you
don’t know where it stands, so from testing reaching an average of about $0.07 per
model-assisted analysis is a measurable difference we can identify thanks to the
observability stack.

## What surprised you?

The ease of using redpanda connect was surprising. I found very descriptive
documentation that lended me a hefty hand when implementing the portions using bloblang
for the filtering process. See [Redpanda Connect Bloblang documentation](https://docs.redpanda.com/connect/guides/bloblang/about/).
Connecting the pipeline to an external stream and local
compute was incredibly easy. Overall the speed to get out the door using redpanda
connect surprised me\!
The delivery and failure handling design was deeply shaped by Redpanda Connect's
[message delivery semantics](https://docs.redpanda.com/connect/guides/delivery_semantics/)
and [error-handling documentation](https://docs.redpanda.com/connect/configuration/error_handling/).

## Where would this break in production?

Based on my testing, I would assume this would break throughput wise. The throughput is
reliant on local compute which can be a limiting factor if we wanted to process more
packages. I do believe that could be solved in a vertical compute manner, dedicating
more cpu and ram would solve it quite easily. Besides the compute limiting factor, I
would think first it’d break the bank. I would want to continue driving the cost per
record that conducts reasoning down. The purpose of this would be to enable more
throughput at a lower cost. Averaging about $0.07 per model-assisted analysis can become
a costly worst case covering 12-15k packages a day. Though the percentage of throughput
that enters reasoning would be minimal due to the deterministic side of the product.

## Why this matters?

In this day and age we have large package maintainers running at full speed, publishing
ground breaking findings, clearing out bugs, and most overlooked, creating new bugs.
It’s time consuming for your engineering team to look through every single package
change before updating. There’s also a risk in not updating and having a vulnerability
catch up to you. Our product provides engineering teams a window to understand package
updates that’ll save your engineering team from getting paged because of a new
unexpected behavior from a package. The triage period for unexpected behaviors is costly
and deadly, but with this product your engineering team can at a glance understand the
potential damage a package update can make and decide if they want to update or not. An
example of this is with Requests 2.32.3, which fixed an issue introduced in 2.32.2
(14.6M+ downloads) but could reintroduce unwarranted behavior if your engineering team
accounted for the prior release already. With this new tool your dependencies can stay
up to date confidently and stay focused on their product mission instead of external
dependency issues.

## Related documents

- [Implementation guide](docs/IMPLEMENTATION.md) — Describes the product boundary,
  architecture, record path, demo canaries, delivery guarantees, observability, and
  take-home limitations.
- [Model cost optimization](docs/COST_OPTIMIZATION.md) — Summarizes the cost-reduction
  journey, deterministic routing strategy, measured model baseline, and remaining cost
  levers.
- [Reasoning worker data path](services/reasoning/src/reasoning_worker/DATA_PATH.md) —
  Provides a concise, file-linked walkthrough of how one release moves through evidence
  collection, routing, analysis, and terminal delivery.
