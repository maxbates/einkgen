import { Construct } from 'constructs';
import { SecretValue } from 'aws-cdk-lib';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';

export class EinkgenSecrets extends Construct {
  public readonly openaiApiKey: secretsmanager.Secret;
  public readonly deviceStatusToken: secretsmanager.Secret;

  constructor(scope: Construct, id: string) {
    super(scope, id);

    // Plaintext placeholder. Operator overwrites via:
    //   aws secretsmanager put-secret-value --secret-id einkgen/openai_api_key \
    //     --secret-string "sk-..."
    // We use unsafePlainText for the placeholder so CloudFormation creates the
    // secret deterministically; the value is rotated by operator post-deploy.
    this.openaiApiKey = new secretsmanager.Secret(this, 'OpenAiApiKey', {
      secretName: 'einkgen/openai_api_key',
      description: 'OpenAI API key for the generator Lambda. Rotate by put-secret-value.',
      secretStringValue: SecretValue.unsafePlainText('REPLACE_ME_POST_DEPLOY'),
    });

    this.deviceStatusToken = new secretsmanager.Secret(this, 'DeviceStatusToken', {
      secretName: 'einkgen/device_status_token',
      description: 'Shared secret validated by the device-status Lambda X-Device-Token header.',
      secretStringValue: SecretValue.unsafePlainText('REPLACE_ME_POST_DEPLOY'),
    });
  }
}
