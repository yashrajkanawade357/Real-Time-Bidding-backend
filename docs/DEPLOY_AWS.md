# Deploying on AWS

Two routes. Route 1 is what runs the public demo: one script, about 15
minutes, HTTPS included. Route 2 is the production-shaped setup with managed
Postgres and an autoscaling service.

## Route 1: one EC2 instance behind CloudFront, set up by its own boot script

```
Internet ──HTTPS──▶ CloudFront ──▶ EC2 (port 80, CloudFront addresses only)
                                   └─ nginx ─▶ API instance 1 / instance 2 ─▶ PostgreSQL
```

Everything runs on one machine, and the machine sets itself up: no SSH, no
manual installs. Good for a public demo; for durable data use RDS (Route 2).

- **First boot**: [`deploy/ec2-user-data.sh`](../deploy/ec2-user-data.sh)
  installs Docker, clones this repository, installs a per-boot hook, and runs
  [`deploy/on-boot.sh`](../deploy/on-boot.sh).
- **Every boot**: `on-boot.sh` generates the database password and admin key
  into a root-only `.env` if they don't exist yet, pulls the latest code, and
  starts the stack with [`deploy/docker-compose.aws.yml`](../deploy/docker-compose.aws.yml):
  nginx on port 80 in front of two API instances, nothing else published,
  public lot creation and the unsafe race-demo endpoint off.
- **CI boots exactly this stack** on every push (the `aws-stack` job) and checks
  load balancing, closed ports, locked-down endpoints and security headers.

### From AWS CloudShell: one script

[`deploy/aws-launch.sh`](../deploy/aws-launch.sh) does all of it. Open
**CloudShell** from the console (it's already signed in, in the console's
region) and run:

```bash
curl -fsSLO https://raw.githubusercontent.com/yashrajkanawade357/Real-Time-Bidding-backend/main/deploy/aws-launch.sh
bash aws-launch.sh check     # read-only: region, free-tier size, image, firewall and HTTPS plan
bash aws-launch.sh launch    # firewall + instance + CloudFront; prints the https:// URL
```

`launch`:

1. picks the first **free-tier eligible** size your account allows (t3.micro if available);
2. resolves the current Ubuntu 24.04 image from Canonical's public parameter;
3. creates a security group whose only rule is **port 80 from CloudFront's
   origin-facing prefix list** - the server can't be reached around HTTPS,
   and Postgres and SSH aren't reachable at all;
4. starts the instance with `ec2-user-data.sh` as its user data (IMDSv2 required);
5. creates a CloudFront distribution in front of it - or, if one already
   exists, points it at the new instance, so the `https://` URL never changes -
   with HTTP redirected to HTTPS and caching off (every request is live data).

CloudFront takes 3-10 minutes to deploy and the instance about 5 minutes to
build. Then:

```bash
bash aws-launch.sh status     # instance, URL, health check, end of the boot log
bash aws-launch.sh admin-key  # the access key for https://<your-domain>/admin
bash aws-launch.sh update     # after pushing new code: reboot, pull, rebuild (same URL)
bash aws-launch.sh destroy    # terminate the instance; CloudFront and the firewall are kept
```

**The admin key** is generated on the instance and never leaves it except
through the instance's console log, which only principals in your AWS account
can read; `admin-key` reads it from there. See [SECURITY.md](SECURITY.md) for
why, and what a production setup would use instead.

**Accounts from AWS's new sign-up ("projects")** can only run EC2 in the region
chosen for the project; other regions are denied by an AWS-managed service
control policy. CloudShell opens in the console's region, which is the
project's region, so the script works there as-is. The console URL shows it,
e.g. `...console.aws.amazon.com/console/home?region=ap-southeast-2`.

**To ship new code**: push to `main`, then `bash aws-launch.sh update`. The
instance reboots, pulls and rebuilds on the way up - about three minutes of
downtime, same URL.

`DEMO_RESTOCK` stays on, so whoever opens the link always finds lots open.

## Route 2: ECS Fargate + Application Load Balancer + RDS PostgreSQL

```
Internet ─▶ ALB (HTTP/HTTPS, WebSocket upgrade) ─▶ ECS service: 2+ Fargate tasks ─▶ RDS PostgreSQL
                                                     (each task LISTENs on auction_events)
```

### 1. Database: RDS for PostgreSQL

- Engine PostgreSQL 16 or 17, `db.t4g.micro`, private subnets, no public access.
- Security group: allow `5432` **only from the ECS tasks' security group**.
- Store the connection string in **Secrets Manager** as
  `postgresql://<user>:<password>@<endpoint>:5432/bidding` (create the
  `bidding` database once, or set it as the initial database name).

Nothing else is needed. The app runs its migrations on startup, guarded by an
advisory lock so parallel tasks don't collide.

### 2. Image: ECR

```bash
aws ecr create-repository --repository-name realtime-bidding
aws ecr get-login-password | docker login --username AWS --password-stdin <account>.dkr.ecr.<region>.amazonaws.com
docker build -t realtime-bidding .
docker tag realtime-bidding <account>.dkr.ecr.<region>.amazonaws.com/realtime-bidding:latest
docker push <account>.dkr.ecr.<region>.amazonaws.com/realtime-bidding:latest
```

### 3. Service: ECS on Fargate

- Task definition: the image above, container port `8000`, 0.25 vCPU / 0.5 GB.
- Environment: `DATABASE_URL` from the Secrets Manager secret,
  `ENABLE_UNSAFE_DEMO=false`, `DEMO_RESTOCK=true` for a public demo, and
  `DB_POOL_MAX` sized so that
  `tasks × DB_POOL_MAX + tasks` (one listener connection each) stays under
  RDS's `max_connections`.
- Service: **desired count 2**, in private subnets, attached to the target
  group below.

### 4. Load balancer: ALB

- Target group: protocol HTTP, port `8000`, target type `ip`, health check
  path `/health`.
- Listener: HTTP `80` (and HTTPS `443` with an ACM certificate, which gives
  the browser `wss://` automatically).
- **WebSockets:** the ALB supports the upgrade natively; no extra settings.
- **Idle timeout:** the default 60 s is fine. uvicorn pings every socket every
  20 s, so an idle connection is never cut.
- **Stickiness: leave it off.** A client can reconnect to any task, because
  state lives in RDS and every task receives every event over LISTEN/NOTIFY.

### 5. Verify

```bash
# against the ALB DNS name
python scripts/reconnect_demo.py --base-url http://<alb-dns-name>
```

Open the site in two browsers. With two tasks running, the ALB spreads them
across tasks, yet each still sees the other's bids live. That is the
LISTEN/NOTIFY fan-out working across instances.

## Notes

- **Connection proxies (RDS Proxy, PgBouncer in transaction mode):** fine for
  the bid pool, but the `LISTEN` connection needs a long-lived session of its
  own, so point it at the database endpoint directly.
- **Observability:** FastAPI has built-in OpenTelemetry support. Install
  `fastapi[opentelemetry]` and set `OTEL_EXPORTER_OTLP_ENDPOINT` to an ADOT
  collector, and request traces and metrics land in CloudWatch / X-Ray. Uvicorn logs go to CloudWatch Logs via the
  `awslogs` driver.
- **Cost:** stop or delete the RDS instance, ECS service and ALB when you're
  done. The ALB and RDS bill hourly even when idle.
