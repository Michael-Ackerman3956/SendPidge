#!/usr/bin/env bash
# Creates the SendPidge RCS testing agent, submits the testing registration, invites testers.
# Run once from a shell with AWS credentials; every value comes from the environment, nothing is hard-coded.
# Required: RCS_BRAND_NAME RCS_CONTACT_EMAIL RCS_CONTACT_PHONE RCS_PRIVACY_URL RCS_TERMS_URL RCS_LOGO_PATH RCS_BANNER_PATH RCS_TESTER_NUMBERS (space-separated E.164)
# Optional: AWS_REGION (default us-east-1) RCS_ACCENT_COLOR (default #257A66) RCS_AGENT_ID (reuse an existing agent)
#           RCS_REGISTRATION_ID (reuse an unsubmitted registration already associated with that agent)
#           RCS_TWO_WAY_TOPIC_ARN + RCS_TWO_WAY_ROLE_ARN (stack outputs RcsInboundTopicArn / RcsTwoWayRoleArn): enables two-way on the agent
set -euo pipefail

region="${AWS_REGION:-us-east-1}"
accent_color="${RCS_ACCENT_COLOR:-#257A66}"  # darker than the PWA teal: Google requires 4.5:1 on white, #3AA68E was denied at 2.99
required_settings=(RCS_BRAND_NAME RCS_CONTACT_EMAIL RCS_CONTACT_PHONE RCS_PRIVACY_URL RCS_TERMS_URL RCS_LOGO_PATH RCS_BANNER_PATH RCS_TESTER_NUMBERS)
for setting in "${required_settings[@]}"; do
  [[ -n "${!setting:-}" ]] || { echo "missing $setting" >&2; exit 1; }
done

sms() { aws pinpoint-sms-voice-v2 --region "$region" --output json "$@"; }

if [[ -z "${RCS_AGENT_ID:-}" ]]; then
  agent_json=$(sms create-rcs-agent)
  RCS_AGENT_ID=$(jq -r '.RcsAgentId' <<<"$agent_json")
  sms update-rcs-agent --rcs-agent-id "$RCS_AGENT_ID" --deletion-protection-enabled >/dev/null
  echo "agent created: $RCS_AGENT_ID (waiting 120 s before testers can be added)"
  agent_created_at=$(date +%s)
fi
agent_arn=$(sms describe-rcs-agents --rcs-agent-ids "$RCS_AGENT_ID" | jq -r '.RcsAgents[0].RcsAgentArn')

if [[ -z "${RCS_REGISTRATION_ID:-}" ]]; then
  RCS_REGISTRATION_ID=$(sms create-registration --registration-type TEST_RCS_LAUNCH_REGISTRATION | jq -r '.RegistrationId')
  sms create-registration-association --registration-id "$RCS_REGISTRATION_ID" --resource-id "$RCS_AGENT_ID" >/dev/null
fi
registration_id="$RCS_REGISTRATION_ID"
registration_status=$(sms describe-registrations --registration-ids "$registration_id" | jq -r '.Registrations[0].RegistrationStatus')
if [[ "$registration_status" == "REQUIRES_UPDATES" ]]; then
  sms create-registration-version --registration-id "$registration_id" >/dev/null
  echo "denied version reopened as a new draft: every field is re-entered below"
fi
logo_attachment_id=$(sms create-registration-attachment --attachment-body "fileb://$RCS_LOGO_PATH" | jq -r '.RegistrationAttachmentId')
banner_attachment_id=$(sms create-registration-attachment --attachment-body "fileb://$RCS_BANNER_PATH" | jq -r '.RegistrationAttachmentId')

put_text() { sms put-registration-field-value --registration-id "$registration_id" --field-path "$1" --text-value "$2" >/dev/null; }
put_choice() { sms put-registration-field-value --registration-id "$registration_id" --field-path "$1" --select-choices "$2" >/dev/null; }
put_attachment() { sms put-registration-field-value --registration-id "$registration_id" --field-path "$1" --registration-attachment-id "$2" >/dev/null; }

put_text agentDetails.brandName "$RCS_BRAND_NAME"
put_text agentDetails.serviceName "SendPidge"
put_text agentDetails.senderDisplayName "SendPidge"
put_choice agentDetails.useCase TRANSACTIONAL
put_text agentDetails.agentDescription "Asks a family member whether an email claiming to be them is real."
put_attachment agentDetails.logoImage "$logo_attachment_id"
put_attachment agentDetails.bannerImage "$banner_attachment_id"
put_text agentDetails.accentColor "$accent_color"
put_text agentDetails.contactPhoneNumber "$RCS_CONTACT_PHONE"
put_text agentDetails.contactPhoneLabel "Support"
put_text agentDetails.contactEmailAddress "$RCS_CONTACT_EMAIL"
put_text agentDetails.contactEmailLabel "Support"
put_text agentDetails.privacyPolicyUrl "$RCS_PRIVACY_URL"
put_text agentDetails.termsAndConditionsUrl "$RCS_TERMS_URL"
put_choice agentDetails.billingCategory CONVERSATIONAL
put_choice agentDetails.averageMonthlyRcsFrequency "10"
put_text agentDetails.monthlyRcsVolume "100"

sms submit-registration-version --registration-id "$registration_id" >/dev/null
echo "registration submitted: $registration_id"

for attempt in $(seq 1 20); do
  status=$(sms describe-rcs-agents --rcs-agent-ids "$RCS_AGENT_ID" | jq -r '.RcsAgents[0].TestingAgent.Status // "PENDING"')
  echo "testing agent status: $status"
  [[ "$status" == "ACTIVE" ]] && break
  sleep 15
done
if [[ "$status" != "ACTIVE" ]]; then
  sms describe-registration-field-values --registration-id "$registration_id" | jq '.RegistrationFieldValues[] | select(.DeniedReason) | {FieldPath, DeniedReason}'
  echo "not ACTIVE after 5 min; fix the denied fields, then create a new registration version (all fields must be re-entered)" >&2
  exit 1
fi

if [[ -n "${agent_created_at:-}" ]]; then
  while (( $(date +%s) - agent_created_at < 120 )); do sleep 5; done
fi
for tester_number in $RCS_TESTER_NUMBERS; do
  sms create-verified-destination-number --destination-phone-number "$tester_number" --rcs-agent-id "$RCS_AGENT_ID" >/dev/null
  echo "tester invited: $tester_number (invite from 'RBM Tester Management' arrives in 2-20 min; on iPhone check Unknown Senders)"
done

if [[ -n "${RCS_TWO_WAY_TOPIC_ARN:-}" && -n "${RCS_TWO_WAY_ROLE_ARN:-}" ]]; then
  sms update-rcs-agent --rcs-agent-id "$RCS_AGENT_ID" --two-way-enabled \
    --two-way-channel-arn "$RCS_TWO_WAY_TOPIC_ARN" --two-way-channel-role "$RCS_TWO_WAY_ROLE_ARN" >/dev/null
  echo "two-way enabled → $RCS_TWO_WAY_TOPIC_ARN"
fi

echo "RCS_AGENT_ID=$RCS_AGENT_ID"
echo "RCS_AGENT_ARN=$agent_arn"
echo "next: put both into infra/cdk.json (rcsAgentId, rcsAgentArn), set rcsEnabled true, redeploy"
