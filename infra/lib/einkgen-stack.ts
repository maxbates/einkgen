import { Construct } from 'constructs';
import * as path from 'path';
import * as fs from 'fs';
import { CfnOutput, Duration, Fn, Stack, StackProps } from 'aws-cdk-lib';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
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

    // Generation + device-poll cadence — single knob. The value here
    // drives BOTH:
    //   • the EventBridge rule that fires the generator (one image-gen
    //     call per tick), and
    //   • the ``EINKGEN_POLL_INTERVAL_SECONDS`` env var on the
    //     generator + inbound-email Lambdas, which determines the
    //     ``next_check_after`` hint in each manifest the device fetches.
    //
    // Coupling them eliminates the "edit two files in lockstep" footgun
    // — there's no point polling more often than cron renders, or
    // rendering more often than the device picks up. The firmware's
    // ``SLEEP_MAX_SECONDS = 1 h`` is a cap, not a target: any cadence
    // ≤ 3600 s is honoured directly with no firmware re-flash.
    // Cadences > 3600 s (e.g. 3 h) require also bumping the firmware
    // constant — see QUICKSTART §3.12.
    //
    // Default lives in cdk.json (``einkgenPollIntervalSeconds``); a
    // per-deploy override is ``-c einkgenPollIntervalSeconds=<seconds>``.
    const DEFAULT_POLL_INTERVAL_SECONDS = 1800; // 30 minutes
    const pollIntervalCtx = this.node.tryGetContext('einkgenPollIntervalSeconds');
    let pollIntervalSeconds = DEFAULT_POLL_INTERVAL_SECONDS;
    if (pollIntervalCtx !== undefined && pollIntervalCtx !== null && `${pollIntervalCtx}`.trim()) {
      const parsed = Number(`${pollIntervalCtx}`.trim());
      if (!Number.isInteger(parsed) || parsed <= 0) {
        throw new Error(
          `einkgenPollIntervalSeconds must be a positive integer (got ${pollIntervalCtx})`,
        );
      }
      pollIntervalSeconds = parsed;
    }
    // EventBridge rate() only accepts whole minutes/hours/days, and the
    // minimum is 1 minute. Catch sub-minute or non-minute-aligned values
    // at synth so the deploy doesn't blow up midway through.
    if (pollIntervalSeconds < 60 || pollIntervalSeconds % 60 !== 0) {
      throw new Error(
        `einkgenPollIntervalSeconds must be at least 60 and a multiple of 60 (got ${pollIntervalSeconds})`,
      );
    }

    const lambdas = new EinkgenLambdas(this, 'Lambdas', {
      bucket: bucket.bucket,
      distribution: cdn.distribution,
      cdnBase,
      openaiApiKey: secrets.openaiApiKey,
      deviceStatusToken: secrets.deviceStatusToken,
      adminPassword: secrets.adminPassword,
      adminCookieSigningKey: secrets.adminCookieSigningKey,
      pollIntervalSeconds,
    });

    // Front the admin API on the same origin as the SPA so the session
    // cookie can use SameSite=Lax (Safari and Firefox both block third-party
    // cookies even with SameSite=None, which would lock out the SPA on
    // those browsers). The API Gateway HTTP API URL has the form
    // `https://<apiId>.execute-api.<region>.amazonaws.com` — strip the
    // scheme so HttpOrigin gets the bare hostname it expects.
    const adminApiHost = Fn.select(
      2,
      Fn.split('/', lambdas.adminApiUrl),
    );
    cdn.distribution.addBehavior(
      'admin/*',
      new origins.HttpOrigin(adminApiHost, {
        protocolPolicy: cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
        // The admin Lambda already chooses a tight timeout; keep CloudFront
        // closer to that so a stuck handler doesn't tie up viewers for the
        // 30 s CloudFront default.
        readTimeout: Duration.seconds(30),
      }),
      {
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        // Admin responses are never cacheable (401/204/JSON of just-enqueued
        // ids) and the requests carry cookies/JSON bodies that vary per
        // viewer. Disabling cache also keeps Set-Cookie from getting eaten.
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        // Forward Cookie + Authorization + body to origin. Drops the Host
        // header so API Gateway sees its own hostname (otherwise it returns
        // 403 because the host doesn't match an attached domain).
        originRequestPolicy:
          cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
      },
    );

    // ---- Daily render-cap alarm wiring --------------------------------
    // `einkgenDailyRenderCap` is the threshold for the CloudWatch alarm
    // on generator invocations over a rolling 24 h window. Default 100
    // (~$4/day at gpt-image-2 medium). `einkgenAlarmEmail`, if set,
    // subscribes that address to the alarm SNS topic.
    const DEFAULT_DAILY_RENDER_CAP = 100;
    const dailyRenderCapCtx = this.node.tryGetContext('einkgenDailyRenderCap');
    let dailyRenderCap = DEFAULT_DAILY_RENDER_CAP;
    if (
      dailyRenderCapCtx !== undefined &&
      dailyRenderCapCtx !== null &&
      `${dailyRenderCapCtx}`.trim()
    ) {
      const parsed = Number(`${dailyRenderCapCtx}`.trim());
      if (!Number.isInteger(parsed) || parsed <= 0) {
        throw new Error(
          `einkgenDailyRenderCap must be a positive integer (got ${dailyRenderCapCtx})`,
        );
      }
      dailyRenderCap = parsed;
    }
    const alarmEmailCtx = this.node.tryGetContext('einkgenAlarmEmail');
    const alarmEmail =
      typeof alarmEmailCtx === 'string' && alarmEmailCtx.trim()
        ? alarmEmailCtx.trim()
        : undefined;

    new EinkgenObservability(this, 'Observability', {
      envName: props.envName,
      generator: lambdas.generator,
      readApi: lambdas.readApi,
      deviceStatus: lambdas.deviceStatus,
      adminApi: lambdas.adminApi,
      dailyRenderCap,
      ...(alarmEmail ? { alarmEmail } : {}),
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
        pollIntervalSeconds,
        // Wired so the NOW-subject trigger can async-invoke render_now
        // on the generator instead of waiting for the next cron tick.
        generator: lambdas.generator,
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
    new CfnOutput(this, 'AdminApiUrl', { value: lambdas.adminApiUrl });
    new CfnOutput(this, 'OpenAiSecretName', { value: secrets.openaiApiKey.secretName });
    new CfnOutput(this, 'DeviceStatusSecretName', {
      value: secrets.deviceStatusToken.secretName,
    });
    new CfnOutput(this, 'AdminPasswordSecretName', {
      value: secrets.adminPassword.secretName,
    });
  }
}
