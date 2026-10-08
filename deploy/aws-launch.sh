#!/usr/bin/env bash
# Run the Bidding Floor on one EC2 instance behind CloudFront (HTTPS).
# Meant for AWS CloudShell, which is already signed in and set to the console's region.
#
#   bash aws-launch.sh check      read-only: what would be launched, and where
#   bash aws-launch.sh launch     firewall + instance + CloudFront; prints the https:// URL
#   bash aws-launch.sh status     instance, URL, health check, end of the boot log
#   bash aws-launch.sh admin-key  the admin portal's access key (from the instance's console log)
#   bash aws-launch.sh update     reboot: the instance pulls the latest code and rebuilds
#   bash aws-launch.sh destroy    terminate the instance (CloudFront and the firewall are kept)
#
# The instance sets itself up from deploy/ec2-user-data.sh and deploy/on-boot.sh.
# Port 80 accepts traffic only from CloudFront's origin-facing addresses, so the
# server can't be reached around HTTPS.
set -euo pipefail

MODE="${1:-check}"
NAME="bidding-floor"
REPO_RAW="https://raw.githubusercontent.com/yashrajkanawade357/Real-Time-Bidding-backend/${REF:-main}"
CLOUDFRONT_PREFIX_LIST="com.amazonaws.global.cloudfront.origin-facing"
CACHING_DISABLED="4135ea2d-6df8-44a3-9df3-4b5a84be39ad"   # AWS managed cache policy
ALL_VIEWER="216adef6-5c7f-47e4-b989-5492eafa07d3"         # AWS managed origin request policy
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

instance_dns() {
  aws ec2 describe-instances --instance-ids "$1" \
    --query 'Reservations[0].Instances[0].PublicDnsName' --output text
}

distribution_id() {
  aws cloudfront list-distributions \
    --query "DistributionList.Items[?Comment=='$NAME'].Id | [0]" --output text 2>/dev/null || echo None
}

distribution_domain() {
  aws cloudfront get-distribution --id "$1" --query 'Distribution.DomainName' --output text
}

# Port 80 from CloudFront only. Also removes the open-to-the-world rules an
# older version of this script created.
lock_firewall() {
  local sg="$1" pl
  pl=$(aws ec2 describe-managed-prefix-lists --filters "Name=prefix-list-name,Values=$CLOUDFRONT_PREFIX_LIST" \
    --query 'PrefixLists[0].PrefixListId' --output text)
  aws ec2 authorize-security-group-ingress --group-id "$sg" --ip-permissions \
    "IpProtocol=tcp,FromPort=80,ToPort=80,PrefixListIds=[{PrefixListId=$pl,Description=CloudFront}]" \
    >/dev/null 2>&1 || true
  for port in 80 8001; do
    aws ec2 revoke-security-group-ingress --group-id "$sg" --ip-permissions \
      "IpProtocol=tcp,FromPort=$port,ToPort=$port,IpRanges=[{CidrIp=0.0.0.0/0}]" >/dev/null 2>&1 || true
  done
}

# Create the distribution, or point the existing one at the current instance.
point_cloudfront() {
  local origin="$1" id cfg etag
  id=$(distribution_id)
  if [ "$id" = "None" ] || [ -z "$id" ]; then
    cfg=$(mktemp)
    cat > "$cfg" <<JSON
{
  "CallerReference": "$NAME-$(date +%s)",
  "Comment": "$NAME",
  "Enabled": true,
  "HttpVersion": "http2and3",
  "PriceClass": "PriceClass_All",
  "Origins": {"Quantity": 1, "Items": [{
    "Id": "ec2",
    "DomainName": "$origin",
    "CustomOriginConfig": {
      "HTTPPort": 80, "HTTPSPort": 443, "OriginProtocolPolicy": "http-only",
      "OriginSslProtocols": {"Quantity": 1, "Items": ["TLSv1.2"]},
      "OriginReadTimeout": 60, "OriginKeepaliveTimeout": 5
    }
  }]},
  "DefaultCacheBehavior": {
    "TargetOriginId": "ec2",
    "ViewerProtocolPolicy": "redirect-to-https",
    "AllowedMethods": {"Quantity": 7, "Items": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
      "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]}},
    "CachePolicyId": "$CACHING_DISABLED",
    "OriginRequestPolicyId": "$ALL_VIEWER",
    "Compress": true
  }
}
JSON
    id=$(aws cloudfront create-distribution --distribution-config "file://$cfg" \
      --query 'Distribution.Id' --output text)
    say "CloudFront: created $id" >&2
  else
    cfg=$(mktemp)
    etag=$(aws cloudfront get-distribution-config --id "$id" --query ETag --output text)
    aws cloudfront get-distribution-config --id "$id" --query DistributionConfig --output json \
      | jq --arg o "$origin" '.Origins.Items[0].DomainName = $o' > "$cfg"
    aws cloudfront update-distribution --id "$id" --if-match "$etag" \
      --distribution-config "file://$cfg" >/dev/null
    say "CloudFront: $id now points at $origin" >&2
  fi
  echo "$id"
}

case "$MODE" in
  check|launch)
    say "Region:     $AWS_REGION"
    TYPE=$(pick_type)
    case "$TYPE" in
      none:*) say "No free-tier size among t3/t2/t4g micro/small. Eligible here: ${TYPE#none:}"; exit 1;;
    esac
    ARCH=$(arch_of "$TYPE")
    AMI=$(ami_for "$ARCH")
    VPC=$(default_vpc)
    SG=$(find_sg "$VPC")
    DIST=$(distribution_id)
    say "Size:       $TYPE ($ARCH, free-tier eligible)"
    say "Image:      $AMI (Ubuntu 24.04)"
    say "Network:    $VPC"
    if [ "$SG" = "None" ]; then
      say "Firewall:   will create '$NAME': port 80 from CloudFront only"
    else
      say "Firewall:   $SG (will be locked to CloudFront only)"
    fi
    if [ "$DIST" = "None" ] || [ -z "$DIST" ]; then
      say "HTTPS:      will create a CloudFront distribution"
    else
      say "HTTPS:      CloudFront $DIST ($(distribution_domain "$DIST")) will point at the new instance"
    fi

    EXISTING=$(instance_id)
    if [ -n "$EXISTING" ]; then
      say "Instance:   $EXISTING already exists. Run 'status', or 'destroy' first to replace it."
      exit 0
    fi
    if [ "$MODE" = "check" ]; then
      say "Nothing was created. Run 'launch' to start it."
      exit 0
    fi

    if [ "$SG" = "None" ]; then
      SG=$(aws ec2 create-security-group --group-name "$NAME" --vpc-id "$VPC" \
        --description "Bidding Floor: HTTP from CloudFront only" --query GroupId --output text)
      say "Firewall:   created $SG"
    fi
    lock_firewall "$SG"

    USER_DATA=$(mktemp)
    curl -fsSL "$REPO_RAW/deploy/ec2-user-data.sh" -o "$USER_DATA"
    ID=$(aws ec2 run-instances --image-id "$AMI" --instance-type "$TYPE" \
      --security-group-ids "$SG" --user-data "file://$USER_DATA" \
      --metadata-options HttpTokens=required \
      --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" \
      --query 'Instances[0].InstanceId' --output text)
    say "Instance:   $ID starting"
    aws ec2 wait instance-running --instance-ids "$ID"
    DNS=$(instance_dns "$ID")

    DIST=$(point_cloudfront "$DNS")
    DOMAIN=$(distribution_domain "$DIST")
    say ""
    say "Your site:  https://$DOMAIN"
    say "            (admin portal: https://$DOMAIN/admin)"
    say ""
    say "Waiting for CloudFront to deploy (usually 3-10 minutes)..."
    aws cloudfront wait distribution-deployed --id "$DIST"
    say "CloudFront is deployed. The instance needs about 5 minutes from launch to build;"
    say "check with: bash aws-launch.sh status"
    ;;

  status)
    ID=$(instance_id)
    if [ -z "$ID" ]; then say "No '$NAME' instance in $AWS_REGION."; exit 0; fi
    STATE=$(aws ec2 describe-instances --instance-ids "$ID" \
      --query 'Reservations[0].Instances[0].State.Name' --output text)
    say "Instance:   $ID ($STATE)"
    DIST=$(distribution_id)
    if [ "$DIST" != "None" ] && [ -n "$DIST" ]; then
      DOMAIN=$(distribution_domain "$DIST")
      CF_STATE=$(aws cloudfront get-distribution --id "$DIST" --query 'Distribution.Status' --output text)
      say "URL:        https://$DOMAIN   (CloudFront: $CF_STATE)"
      if curl -fsS -m 8 "https://$DOMAIN/health" >/dev/null 2>&1; then
        say "Health:     the app is up"
      else
        say "Health:     not answering yet (still booting, or CloudFront still deploying)"
      fi
    else
      say "URL:        no CloudFront distribution yet - run 'launch'"
    fi
    say "--- end of the boot log:"
    aws ec2 get-console-output --instance-id "$ID" --latest --output text 2>/dev/null \
      | grep -v BIDDING_ADMIN_KEY | tail -15 || true
    ;;

  admin-key)
    ID=$(instance_id)
    if [ -z "$ID" ]; then say "No '$NAME' instance in $AWS_REGION."; exit 1; fi
    KEY=$(aws ec2 get-console-output --instance-id "$ID" --latest --output text 2>/dev/null \
      | grep -o 'BIDDING_ADMIN_KEY=[A-Za-z0-9_-]*' | tail -1 | cut -d= -f2- || true)
    if [ -z "$KEY" ]; then
      say "Not in the console log yet. The instance prints it once setup finishes (about 5 minutes after launch)."
      exit 1
    fi
    say "Admin access key (keep it private; anyone with it can manage lots):"
    say "  $KEY"
    ;;

  update)
    ID=$(instance_id)
    if [ -z "$ID" ]; then say "No '$NAME' instance in $AWS_REGION. Run 'launch' first."; exit 0; fi
    aws ec2 reboot-instances --instance-ids $ID
    say "Rebooting $ID. On the way up it pulls the latest code from GitHub and rebuilds."
    say "Back in about 3 minutes, same URL. Check with: bash aws-launch.sh status"
    ;;

  destroy)
    ID=$(instance_id)
    if [ -z "$ID" ]; then say "Nothing to terminate."; exit 0; fi
    aws ec2 terminate-instances --instance-ids $ID >/dev/null
    say "Terminating $ID. It stops costing anything once it is gone."
    say "CloudFront and the firewall are kept; 'launch' reuses them, so the https:// URL stays the same."
    ;;

  *)
    say "usage: bash aws-launch.sh check|launch|status|admin-key|update|destroy"
    exit 2
    ;;
esac
