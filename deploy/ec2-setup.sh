#!/usr/bin/env bash
# One-shot deploy of PlanBot to a single small EC2 box. Run it in AWS CloudShell (us-east-1) as yourself.
# It asks for the Discord values at prompts; the token is typed hidden and stored only as an SSM SecureString,
# never in this repo, the instance's user-data, or chat. No inbound ports are opened and no SSH keys exist:
# you reach the box through Session Manager. Bedrock is called with the instance role, so no AWS keys sit on it.
# Tear everything down with deploy/ec2-teardown.sh.
set -euo pipefail
REGION=us-east-1
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
echo "Deploying PlanBot into account $ACCOUNT, region $REGION"

read -rp "Discord application ID: " APP_ID
read -rp "Discord server (guild) ID: " GUILD_ID
read -rp "Discord channel ID: " CHANNEL_ID
read -rsp "Discord bot token (hidden): " TOKEN; echo
read -rp "Bedrock model id [us.anthropic.claude-haiku-4-5-20251001-v1:0]: " MODEL
MODEL=${MODEL:-us.anthropic.claude-haiku-4-5-20251001-v1:0}

echo "1/6 storing the app config (with the token) as one encrypted SSM parameter"
aws ssm put-parameter --region $REGION --name /planbot/env --type SecureString --overwrite --value "DISCORD_TOKEN=$TOKEN
DISCORD_APP_ID=$APP_ID
DISCORD_GUILD_ID=$GUILD_ID
DISCORD_CHANNEL_ID=$CHANNEL_ID
LLM_PROVIDER=bedrock
LLM_MODEL=$MODEL
AWS_REGION=$REGION
TZ_NAME=America/Phoenix
DEMO_AREA=Tempe" >/dev/null
unset TOKEN

echo "2/6 IAM role: can invoke Bedrock, read only /planbot/env, and be reached by Session Manager"
aws iam create-role --role-name planbot-ec2-role --assume-role-policy-document '{
  "Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
aws iam put-role-policy --role-name planbot-ec2-role --policy-name planbot-inline --policy-document "{
  \"Version\":\"2012-10-17\",\"Statement\":[
   {\"Effect\":\"Allow\",\"Action\":[\"bedrock:InvokeModel\",\"bedrock:InvokeModelWithResponseStream\"],\"Resource\":\"*\"},
   {\"Effect\":\"Allow\",\"Action\":\"ssm:GetParameter\",\"Resource\":\"arn:aws:ssm:$REGION:$ACCOUNT:parameter/planbot/env\"}]}"
aws iam attach-role-policy --role-name planbot-ec2-role --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam create-instance-profile --instance-profile-name planbot-ec2-profile >/dev/null
aws iam add-role-to-instance-profile --instance-profile-name planbot-ec2-profile --role-name planbot-ec2-role
echo "   waiting for the instance profile to propagate"; sleep 15

echo "3/6 security group with NO inbound rules (Discord is outbound-only)"
VPC=$(aws ec2 describe-vpcs --region $REGION --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
SG=$(aws ec2 create-security-group --region $REGION --group-name planbot-sg --description "PlanBot: no inbound" --vpc-id "$VPC" --query GroupId --output text)

echo "4/6 boot script: install Docker, fetch config, build the image from the public repo, run it"
cat > /tmp/planbot-userdata.sh <<'UD'
#!/bin/bash
set -eux
dnf install -y docker git
systemctl enable --now docker
mkdir -p /opt/planbot/data
aws ssm get-parameter --region us-east-1 --name /planbot/env --with-decryption --query Parameter.Value --output text > /opt/planbot/.env
chmod 600 /opt/planbot/.env
git clone https://github.com/smanika3/PlanBot-DevHacks.git /opt/planbot/src
cd /opt/planbot/src && docker build -t planbot .
docker run -d --restart unless-stopped --name planbot --env-file /opt/planbot/.env -v /opt/planbot/data:/data planbot
UD

echo "5/6 launching one t3.small (IMDS hop limit 2 so the container can use the instance role)"
AMI=$(aws ssm get-parameter --region $REGION --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 --query Parameter.Value --output text)
IID=$(aws ec2 run-instances --region $REGION --image-id "$AMI" --instance-type t3.small --security-group-ids "$SG" \
  --iam-instance-profile Name=planbot-ec2-profile --metadata-options HttpTokens=required,HttpPutResponseHopLimit=2 \
  --user-data file:///tmp/planbot-userdata.sh --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=planbot}]' \
  --query 'Instances[0].InstanceId' --output text)
echo "6/6 done: instance $IID. Give it about 3 minutes to build."
echo "Stop the bot on your laptop first, or two copies will answer. Then check the logs in Session Manager:"
echo "  aws ssm start-session --region $REGION --target $IID    # then: sudo docker logs -f planbot"
