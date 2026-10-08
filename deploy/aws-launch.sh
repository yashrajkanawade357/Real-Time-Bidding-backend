#!/usr/bin/env bash
# Run the Bidding Floor demo on one EC2 instance. Meant for AWS CloudShell,
# which is already signed in and set to the console's region.
#
#   bash aws-launch.sh check     read-only: what would be launched, and where
#   bash aws-launch.sh launch    open ports 80 and 8001, start the instance
#   bash aws-launch.sh status    instance state, URL, health and end of boot log
#   bash aws-launch.sh update    reboot it: it pulls the latest code and rebuilds
#   bash aws-launch.sh destroy   terminate the instance (the firewall is kept)
#
# The instance sets itself up from deploy/ec2-user-data.sh on first boot.
set -euo pipefail

MODE="${1:-check}"
NAME="bidding-floor"
REPO_RAW="https://raw.githubusercontent.com/yashrajkanawade357/Real-Time-Bidding-backend/${REF:-main}"
export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:?set AWS_REGION to the region to use}}"
export AWS_DEFAULT_REGION="$AWS_REGION"

say() { printf '%s\n' "$*"; }

instance_id() {
  aws ec2 describe-instances \
    --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text
}

# First free-tier eligible size from a sensible list (the eligible set differs per account).
pick_type() {
  local free t
  # Text output separates names with tabs and newlines; flatten to single spaces.
  free=$(aws ec2 describe-instance-types --filters Name=free-tier-eligible,Values=true \
    --query 'InstanceTypes[].InstanceType' --output text | tr -s '[:space:]' ' ')
  for t in t3.micro t2.micro t3.small t4g.micro t4g.small; do
    case " $free " in *" $t "*) echo "$t"; return;; esac
  done
  echo "none:$free"
}

arch_of() { case "$1" in *g.*) echo arm64;; *) echo amd64;; esac; }

# Current Ubuntu 24.04 image, from Canonical's public parameter.
ami_for() {
  aws ssm get-parameters \
    --names "/aws/service/canonical/ubuntu/server/24.04/stable/current/$1/hvm/ebs-gp3/ami-id" \
    --query 'Parameters[0].Value' --output text
}

default_vpc() {
  aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text
}

find_sg() {
  aws ec2 describe-security-groups --filters "Name=group-name,Values=$NAME" "Name=vpc-id,Values=$1" \
    --query 'SecurityGroups[0].GroupId' --output text
}

public_ip() {
  aws ec2 describe-instances --instance-ids "$1" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text
}

case "$MODE" in
  check|launch)
    say "Region:   $AWS_REGION"
    TYPE=$(pick_type)
    case "$TYPE" in
      none:*) say "No free-tier size among t3/t2/t4g micro/small. Eligible here: ${TYPE#none:}"; exit 1;;
    esac
    ARCH=$(arch_of "$TYPE")
    AMI=$(ami_for "$ARCH")
    VPC=$(default_vpc)
    SG=$(find_sg "$VPC")
    say "Size:     $TYPE ($ARCH, free-tier eligible)"
    say "Image:    $AMI (Ubuntu 24.04)"
    say "Network:  $VPC"
    if [ "$SG" = "None" ]; then
      say "Firewall: will create '$NAME' with only ports 80 and 8001 open"
    else
      say "Firewall: $SG (already exists)"
    fi

    EXISTING=$(instance_id)
    if [ -n "$EXISTING" ]; then
      say "Instance: $EXISTING already exists. Run 'status' for its URL."
      exit 0
    fi
    if [ "$MODE" = "check" ]; then
      say "Nothing was created. Run 'launch' to start it."
      exit 0
    fi

    if [ "$SG" = "None" ]; then
      SG=$(aws ec2 create-security-group --group-name "$NAME" --vpc-id "$VPC" \
        --description "Bidding Floor demo: HTTP on 80 and 8001" --query GroupId --output text)
      aws ec2 authorize-security-group-ingress --group-id "$SG" --ip-permissions \
        'IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0}]' \
        'IpProtocol=tcp,FromPort=8001,ToPort=8001,IpRanges=[{CidrIp=0.0.0.0/0}]' >/dev/null
      say "Firewall: created $SG"
    fi

    USER_DATA=$(mktemp)
    curl -fsSL "$REPO_RAW/deploy/ec2-user-data.sh" -o "$USER_DATA"
    ID=$(aws ec2 run-instances --image-id "$AMI" --instance-type "$TYPE" \
      --security-group-ids "$SG" --user-data "file://$USER_DATA" \
      --metadata-options HttpTokens=required \
      --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" \
      --query 'Instances[0].InstanceId' --output text)
    say "Instance: $ID starting"
    aws ec2 wait instance-running --instance-ids "$ID"
    IP=$(public_ip "$ID")
    say ""
    say "Running. It needs about 5 minutes to install Docker and build, then open:"
    say "  http://$IP         instance 1"
    say "  http://$IP:8001    instance 2, same database"
    ;;

  status)
    ID=$(instance_id)
    if [ -z "$ID" ]; then say "No '$NAME' instance in $AWS_REGION."; exit 0; fi
    STATE=$(aws ec2 describe-instances --instance-ids "$ID" \
      --query 'Reservations[0].Instances[0].State.Name' --output text)
    IP=$(public_ip "$ID")
    say "Instance: $ID ($STATE)"
    say "URL:      http://$IP"
    if curl -fsS -m 5 "http://$IP/health" >/dev/null 2>&1; then
      say "Health:   the app is up"
    else
      say "Health:   not answering yet (still booting?)"
    fi
    say "--- end of the boot log:"
    aws ec2 get-console-output --instance-id "$ID" --latest --output text 2>/dev/null | tail -15 || true
    ;;

  update)
    ID=$(instance_id)
    if [ -z "$ID" ]; then say "No '$NAME' instance in $AWS_REGION. Run 'launch' first."; exit 0; fi
    aws ec2 reboot-instances --instance-ids $ID
    say "Rebooting $ID. On the way up it pulls the latest code from GitHub and rebuilds."
    say "Back in about 2 minutes, same URL. Check with: bash aws-launch.sh status"
    ;;

  destroy)
    ID=$(instance_id)
    if [ -z "$ID" ]; then say "Nothing to terminate."; exit 0; fi
    aws ec2 terminate-instances --instance-ids $ID >/dev/null
    say "Terminating $ID. It stops costing anything once it is gone."
    ;;

  *)
    say "usage: bash aws-launch.sh check|launch|status|update|destroy"
    exit 2
    ;;
esac
