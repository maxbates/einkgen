import { Construct } from 'constructs';
import * as path from 'path';
import * as fs from 'fs';
import { CfnOutput, Stack, StackProps } from 'aws-cdk-lib';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';

import { EinkgenBucket } from './bucket';
import { EinkgenCdn } from './cloudfront';
import { EinkgenSecrets } from './secrets';
import { EinkgenLambdas } from './lambdas';
import { EinkgenObservability } from './observability';

export interface EinkgenStackProps extends StackProps {
  envName: string;
}

const DEFAULT_PILLOW_LAYER_ARN =
  'arn:aws:lambda:us-east-1:770693421928:layer:Klayers-p312-Pillow:16';

export class EinkgenStack extends Stack {
  constructor(scope: Construct, id: string, props: EinkgenStackProps) {
    super(scope, id, props);

    const pillowLayerArn =
      (this.node.tryGetContext('pillowLayerArn') as string | undefined) ??
      DEFAULT_PILLOW_LAYER_ARN;

    const includeWebAssets =
      this.node.tryGetContext('includeWebAssets') !== false &&
      this.node.tryGetContext('includeWebAssets') !== 'false';

    const bucket = new EinkgenBucket(this, 'Bucket', {
      envName: props.envName,
    });

    const cdn = new EinkgenCdn(this, 'Cdn', {
      bucket: bucket.bucket,
    });

    const secrets = new EinkgenSecrets(this, 'Secrets');

    const cdnBase = `https://${cdn.distribution.distributionDomainName}`;

    const lambdas = new EinkgenLambdas(this, 'Lambdas', {
      bucket: bucket.bucket,
      distribution: cdn.distribution,
      cdnBase,
      openaiApiKey: secrets.openaiApiKey,
      deviceStatusToken: secrets.deviceStatusToken,
      pillowLayerArn,
    });

    new EinkgenObservability(this, 'Observability', {
      envName: props.envName,
      generator: lambdas.generator,
      readApi: lambdas.readApi,
      deviceStatus: lambdas.deviceStatus,
    });

    // Web deployment — gated on the includeWebAssets context flag AND on the
    // physical existence of web/dist/. Track C builds this; we skip cleanly
    // when it hasn't run yet.
    //
    // Cache strategy: the SPA's index.html references hashed asset filenames
    // (Vite emits `assets/index-<hash>.js`). The hashed files are immutable
    // and safe to cache aggressively. index.html itself must NOT be cached
    // long — otherwise a redeploy can leave clients pointing at deleted asset
    // hashes for the CloudFront TTL.
    const webDist = path.resolve(__dirname, '..', '..', 'web', 'dist');
    if (includeWebAssets && fs.existsSync(webDist)) {
      new s3deploy.BucketDeployment(this, 'WebDeploy', {
        sources: [s3deploy.Source.asset(webDist)],
        destinationBucket: bucket.bucket,
        destinationKeyPrefix: 'web/',
        distribution: cdn.distribution,
        distributionPaths: ['/web/*'],
        prune: true,
        cacheControl: [
          s3deploy.CacheControl.fromString('public, max-age=31536000, immutable'),
        ],
      });
      // Override for the SPA shell only — must be revalidated every fetch.
      new s3deploy.BucketDeployment(this, 'WebShellDeploy', {
        sources: [
          s3deploy.Source.asset(webDist, {
            // Only ship index.html in this asset; assets/* came via WebDeploy.
            exclude: ['**/*', '!index.html'],
          }),
        ],
        destinationBucket: bucket.bucket,
        destinationKeyPrefix: 'web/',
        distribution: cdn.distribution,
        distributionPaths: ['/web/index.html'],
        prune: false,
        cacheControl: [
          s3deploy.CacheControl.fromString('public, max-age=0, must-revalidate'),
        ],
      });
    }

    // ---- outputs -----------------------------------------------------
    new CfnOutput(this, 'BucketName', { value: bucket.bucket.bucketName });
    new CfnOutput(this, 'CdnDomain', { value: cdn.distribution.distributionDomainName });
    new CfnOutput(this, 'CdnDistributionId', { value: cdn.distribution.distributionId });
    new CfnOutput(this, 'ReadApiUrl', { value: lambdas.readApiFunctionUrl.url });
    new CfnOutput(this, 'DeviceStatusUrl', { value: lambdas.deviceStatusFunctionUrl.url });
    new CfnOutput(this, 'OpenAiSecretName', { value: secrets.openaiApiKey.secretName });
    new CfnOutput(this, 'DeviceStatusSecretName', {
      value: secrets.deviceStatusToken.secretName,
    });
  }
}
