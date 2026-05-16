import { Construct } from 'constructs';
import * as path from 'path';
import * as fs from 'fs';
import { CfnOutput, Stack, StackProps } from 'aws-cdk-lib';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as route53 from 'aws-cdk-lib/aws-route53';

import { EinkgenBucket } from './bucket';
import { EinkgenCdn } from './cloudfront';
import { EinkgenSecrets } from './secrets';
import { EinkgenLambdas, bundlePython, stagePythonSource } from './lambdas';
import { EinkgenInboundEmail } from './inbound-email';
import { EinkgenObservability } from './observability';

export interface EinkgenStackProps extends StackProps {
  envName: string;
}

export class EinkgenStack extends Stack {
  constructor(scope: Construct, id: string, props: EinkgenStackProps) {
    super(scope, id, props);

    const includeWebAssets =
      this.node.tryGetContext('includeWebAssets') !== false &&
      this.node.tryGetContext('includeWebAssets') !== 'false';

    const bucket = new EinkgenBucket(this, 'Bucket', {
      envName: props.envName,
    });

    // Custom-domain context flags. Both pieces (site + inbound email) can
    // independently point at the same domain; we look up the hosted zone
    // once and share it.
    //   cdk deploy -c einkgenSiteDomain=einkgen.link \
    //              -c einkgenInboundDomain=einkgen.link
    const siteDomainCtx = this.node.tryGetContext('einkgenSiteDomain');
    const inboundDomainCtx = this.node.tryGetContext('einkgenInboundDomain');
    const siteDomain =
      typeof siteDomainCtx === 'string' && siteDomainCtx.trim()
        ? siteDomainCtx.trim()
        : undefined;
    const inboundDomain =
      typeof inboundDomainCtx === 'string' && inboundDomainCtx.trim()
        ? inboundDomainCtx.trim()
        : undefined;
    // Hosted-zone lookup: we need a zone whenever any custom domain is set.
    // If both site + inbound point at the same domain (the common case), we
    // look it up once and reuse. If they're on different domains, we'd need
    // two lookups — out of scope for now since neither has come up.
    let sharedHostedZone: route53.IHostedZone | undefined;
    if (siteDomain || inboundDomain) {
      const lookupName = siteDomain ?? inboundDomain!;
      if (siteDomain && inboundDomain && siteDomain !== inboundDomain) {
        throw new Error(
          `einkgenSiteDomain (${siteDomain}) and einkgenInboundDomain (${inboundDomain}) ` +
            'must currently be the same domain; per-feature distinct domains are not yet supported',
        );
      }
      sharedHostedZone = route53.HostedZone.fromLookup(this, 'SharedHostedZone', {
        domainName: lookupName,
      });
    }

    const cdn = new EinkgenCdn(this, 'Cdn', {
      bucket: bucket.bucket,
      ...(siteDomain && sharedHostedZone
        ? { siteDomain, hostedZone: sharedHostedZone }
        : {}),
    });

    const secrets = new EinkgenSecrets(this, 'Secrets');

    // When a site domain is set, route the device's manifest image_url at
    // the friendly hostname so future-generated manifests reference
    // ``https://einkgen.link/current/image.bmp`` instead of the CloudFront
    // *.cloudfront.net default. Existing device firmware is hardcoded to
    // whatever URL it was flashed with; this only affects URL strings
    // baked into manifests after deploy.
    const cdnBase = siteDomain
      ? `https://${siteDomain}`
      : `https://${cdn.distribution.distributionDomainName}`;

    const lambdas = new EinkgenLambdas(this, 'Lambdas', {
      bucket: bucket.bucket,
      distribution: cdn.distribution,
      cdnBase,
      openaiApiKey: secrets.openaiApiKey,
      deviceStatusToken: secrets.deviceStatusToken,
    });

    new EinkgenObservability(this, 'Observability', {
      envName: props.envName,
      generator: lambdas.generator,
      readApi: lambdas.readApi,
      deviceStatus: lambdas.deviceStatus,
    });

    // ---- Inbound-email submissions (opt-in) -----------------------------
    // Gated on a context flag because SES inbound requires a domain the
    // operator owns. The rest of the stack deploys clean without it.
    //   cdk deploy -c einkgenInboundDomain=submit.example.com
    //   cdk deploy -c einkgenInboundDomain=submit.example.com \
    //              -c einkgenProjectUrl=https://github.com/you/einkgen
    if (inboundDomain && sharedHostedZone) {
      const projectUrl = this.node.tryGetContext('einkgenProjectUrl');
      const sourceStaged = stagePythonSource();

      // Optional first-deploy seed for ``config/email_allowlist.txt``. Comma-
      // separated emails on the CDK CLI — kept OUT of committed code so per-
      // deployment allowlist members don't leak into the repo. After the
      // first deploy, the operator manages the list via ``einkgen allowlist
      // {ls,add,rm}``; the seed only runs once per construct creation.
      //   cdk deploy -c einkgenInboundDomain=einkgen.link \
      //              -c einkgenAllowlistSeed=you@gmail.com,partner@gmail.com
      const seedCtx = this.node.tryGetContext('einkgenAllowlistSeed');
      const seedAllowlist =
        typeof seedCtx === 'string' && seedCtx.trim()
          ? seedCtx
              .split(',')
              .map((e) => e.trim())
              .filter((e) => e.length > 0)
          : undefined;

      new EinkgenInboundEmail(this, 'InboundEmail', {
        bucket: bucket.bucket,
        inboundDomain,
        hostedZone: sharedHostedZone,
        projectUrl: typeof projectUrl === 'string' && projectUrl.trim()
          ? projectUrl.trim()
          : undefined,
        code: bundlePython('requirements-inbound-email.txt', sourceStaged),
        seedAllowlist,
      });
    }

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
    new CfnOutput(this, 'ReadApiUrl', { value: lambdas.readApiUrl });
    new CfnOutput(this, 'DeviceStatusUrl', { value: lambdas.deviceStatusUrl });
    new CfnOutput(this, 'OpenAiSecretName', { value: secrets.openaiApiKey.secretName });
    new CfnOutput(this, 'DeviceStatusSecretName', {
      value: secrets.deviceStatusToken.secretName,
    });
  }
}
