# Deploying on AWS

Two routes. Route 1 gets a public demo running in about 10 minutes. Route 2
is the production-shaped setup with managed Postgres and more than one instance.

## Route 1: one EC2 instance, set up by its own boot script

Everything (Postgres and two API instances) runs on one machine, and the
machine sets itself up on first boot from
[`deploy/ec2-user-data.sh`](../deploy/ec2-user-data.sh). No SSH and no manual
installs. Good for a demo; for durable data use RDS (Route 2).

The boot script installs Docker, clones this repository and runs
`docker compose` with [`deploy/docker-compose.aws.yml`](../deploy/docker-compose.aws.yml).
That puts instance 1 on port **80** and instance 2 on **8001**, keeps Postgres
off the network, and turns off the unsafe race-demo endpoint. CI boots exactly
this stack on every push (the `aws-stack` job).

### From AWS CloudShell

Open CloudShell from the console's top bar, in the region you want (e.g.
Mumbai, `ap-south-1`), and run:

```bash
# Ubuntu 24.04, resolved from Canonical's public parameter (always current)
AMI=$(aws ssm get-parameters   --names /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id   --query 'Parameters[0].Value' --output text)

# Firewall: the two app ports only. Postgres (5432) and SSH (22) stay closed.
VPC=$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
SG=$(aws ec2 create-security-group --group-name bidding-floor   --description "Bidding Floor demo: HTTP only" --vpc-id "$VPC" --query GroupId --output text)
aws ec2 authorize-security-group-ingress --group-id "$SG" --ip-permissions   'IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0}]'   'IpProtocol=tcp,FromPort=8001,ToPort=8001,IpRanges=[{CidrIp=0.0.0.0/0}]'

# Launch with the boot script as user data
curl -fsSLo user-data.sh   https://raw.githubusercontent.com/yashrajkanawade357/Real-Time-Bidding-backend/main/deploy/ec2-user-data.sh
ID=$(aws ec2 run-instances --image-id "$AMI" --instance-type t3.micro   --security-group-ids "$SG" --user-data file://user-data.sh   --metadata-options HttpTokens=required   --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=bidding-floor}]'   --query 'Instances[0].InstanceId' --output text)
aws ec2 wait instance-running --instance-ids "$ID"
aws ec2 describe-instances --instance-ids "$ID"   --query 'Reservations[0].Instances[0].PublicIpAddress' --output text
```

Give it about five minutes to install Docker and build the image, then open
`http://<public-ip>` and `http://<public-ip>:8001` in two windows.

Use a free-tier eligible type for your account; list them with
`aws ec2 describe-instance-types --filters Name=free-tier-eligible,Values=true --query 'InstanceTypes[].InstanceType'`.

**If the page doesn't load**, read the boot log without SSH:
`aws ec2 get-console-output --instance-id "$ID" --latest --output text | tail -50`.

**To ship new code**: user data only runs on an instance's first boot, so
launch a fresh instance with the same `run-instances` command and terminate the
old one. It takes about five minutes, and the data in this demo is disposable.

**To stop paying**: `aws ec2 terminate-instances --instance-ids "$ID"`.

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
