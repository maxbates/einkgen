#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { EinkgenStack } from '../lib/einkgen-stack';

const app = new cdk.App();

const env = (app.node.tryGetContext('env') as string | undefined) ?? 'dev';

new EinkgenStack(app, `EinkgenStack-${env}`, {
  envName: env,
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION ?? 'us-east-1',
  },
  description: `einkgen infrastructure (${env}) — bucket, CloudFront, 3 Lambdas, secrets, observability.`,
});
