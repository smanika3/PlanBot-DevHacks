#!/usr/bin/env bash
# Removes everything deploy/ec2-setup.sh created. Run in AWS CloudShell (us-east-1). Safe to re-run.
REGION=us-east-1
IID=$(aws ec2 describe-instances --region $REGION --filters Name=tag:Name,Values=planbot Name=instance-state-name,Values=pending,running,stopped \
  --query 'Reservations[].Instances[].InstanceId' --output text)
[ -n "$IID" ] && aws ec2 terminate-instances --region $REGION --instance-ids $IID >/dev/null && aws ec2 wait instance-terminated --region $REGION --instance-ids $IID
SG=$(aws ec2 describe-security-groups --region $REGION --filters Name=group-name,Values=planbot-sg --query 'SecurityGroups[0].GroupId' --output text)
[ "$SG" != "None" ] && aws ec2 delete-security-group --region $REGION --group-id "$SG"
aws iam remove-role-from-instance-profile --instance-profile-name planbot-ec2-profile --role-name planbot-ec2-role
aws iam delete-instance-profile --instance-profile-name planbot-ec2-profile
aws iam detach-role-policy --role-name planbot-ec2-role --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam delete-role-policy --role-name planbot-ec2-role --policy-name planbot-inline
aws iam delete-role --role-name planbot-ec2-role
aws ssm delete-parameter --region $REGION --name /planbot/env
echo "PlanBot AWS resources removed."
