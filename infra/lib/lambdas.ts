import { Construct } from 'constructs';
import * as path from 'path';
import * as fs from 'fs';
import { AssetHashType, Duration } from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import {
  HttpApi,
  HttpMethod,
  CorsHttpMethod,
} from 'aws-cdk-lib/aws-apigatewayv2';
import { HttpLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';

export interface EinkgenLambdasProps {
  bucket: s3.Bucket;
  distribution: cloudfront.Distribution;
  cdnBase: string;
  openaiApiKey: secretsmanager.Secret;
  deviceStatusToken: secretsmanager.Secret;
  adminPassword: secretsmanager.Secret;
  adminCookieSigningKey: secretsmanager.Secret;
  /**
   * Single cadence knob, in seconds. Drives BOTH the EventBridge cron
   * rate that fires the generator AND the manifest's
   * ``next_check_after`` hint (via ``EINKGEN_POLL_INTERVAL_SECONDS`` on
   * the generator + inbound-email Lambdas).
   *
   * Values ≤ 3600 are honoured by the firmware directly (its
   * ``SLEEP_MAX_SECONDS = 1 h`` is a cap, not a target). Values > 3600
   * require a firmware re-flash to raise that constant in lockstep —
   * see QUICKSTART §3.12.
   *
   * Default lives in ``einkgen-stack.ts`` (`DEFAULT_POLL_INTERVAL_SECONDS`).
   */
  pollIntervalSeconds: number;
}

// Root of the Python package on disk. Bundling stages a copy under
// infra/lambda/_src/ so the Docker mount of infra/lambda/ contains both
// the requirements files and the einkgen source tree.
const REPO_PYTHON_SRC = path.resolve(__dirname, '..', '..', 'src', 'einkgen');
const STAGED_SRC_DIR = path.resolve(__dirname, '..', 'lambda', '_src');

function copyDirSync(src: string, dest: string): void {
  if (!fs.existsSync(src)) return;
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    if (entry.name === '__pycache__' || entry.name.endsWith('.pyc')) continue;
    const s = path.join(src, entry.name);
    const d = path.join(dest, entry.name);
    if (entry.isDirectory()) {
      copyDirSync(s, d);
    } else if (entry.isFile()) {
      fs.copyFileSync(s, d);
    }
  }
}

function stageSource(): boolean {
  // Returns true if source was staged; false if source dir is absent.
  // The latter is acceptable for `cdk synth` smoke tests in this worktree
  // (contract — other tracks haven't landed yet).
  if (!fs.existsSync(REPO_PYTHON_SRC)) {
    return false;
  }
  // Wipe and re-copy so renames in src/ are reflected.
  fs.rmSync(STAGED_SRC_DIR, { recursive: true, force: true });
  fs.mkdirSync(STAGED_SRC_DIR, { recursive: true });
  copyDirSync(REPO_PYTHON_SRC, path.join(STAGED_SRC_DIR, 'einkgen'));
  return true;
}

// Local bundler fallback used for `cdk synth` smoke-tests where Docker
// isn't available (e.g. parallel-track worktrees without the Docker
// daemon). It copies the staged Python source into the output dir and
// skips the `pip install` step — so the resulting asset is NOT
// deploy-ready, only synth-shaped. Real deploys run the Docker path.
// Sentinel filename — also referenced by each Lambda handler module so a
// runtime import fails fast if a synth-only asset is somehow deployed.
const SYNTH_ONLY_SENTINEL = 'SYNTH_ONLY_DO_NOT_DEPLOY';

function makeLocalBundler(requirementsFile: string, sourceStaged: boolean) {
  return {
    tryBundle(outputDir: string): boolean {
      const skipFlag = process.env.EINKGEN_LOCAL_BUNDLE_SYNTH_ONLY === '1';
      if (!skipFlag) {
        return false;
      }
      void requirementsFile;
      fs.mkdirSync(outputDir, { recursive: true });
      const einkgenOut = path.join(outputDir, 'einkgen');
      fs.mkdirSync(einkgenOut, { recursive: true });
      if (sourceStaged) {
        // staged source lives at infra/lambda/_src/einkgen
        const src = path.join(__dirname, '..', 'lambda', '_src', 'einkgen');
        copyDirSync(src, einkgenOut);
      } else {
        fs.writeFileSync(
          path.join(einkgenOut, '__init__.py'),
          '# synth-only stub; replaced by real bundling on deploy\n',
        );
      }
      // Belt-and-suspenders: even if EINKGEN_LOCAL_BUNDLE_SYNTH_ONLY=1 ever
      // leaks into a real `cdk deploy`, the handler refuses to run.
      fs.writeFileSync(
        path.join(outputDir, SYNTH_ONLY_SENTINEL),
        'This asset was produced by the synth-only local bundler. ' +
          'Re-bundle with Docker before deploying.\n',
      );
      return true;
    },
  };
}

export function stagePythonSource(): boolean {
  return stageSource();
}

export function bundlePython(requirementsFile: string, sourceStaged: boolean): lambda.AssetCode {
  const assetRoot = path.join(__dirname, '..', 'lambda');
  // If source isn't staged (synth smoke test), we still need a bundling
  // command that won't crash. The "cp" line is gated with a test.
  const copyCmd = sourceStaged
    ? 'cp -r /asset-input/_src/einkgen /asset-output/einkgen'
    : 'mkdir -p /asset-output/einkgen && echo "stub" > /asset-output/einkgen/__init__.py';
  return lambda.Code.fromAsset(assetRoot, {
    // Force OUTPUT-content hashing. The default for bundled assets is source-
    // based, which means the synth-only local bundler and the real Docker
    // bundler produce IDENTICAL asset hashes despite their outputs differing
    // by 30 MB of pip-installed deps. cdk-assets then dedups on the hash and
    // refuses to overwrite a previously-uploaded stub zip — Lambdas end up
    // pointing at the stub even after a clean redeploy. Hashing the output
    // keeps the two paths distinct.
    assetHashType: AssetHashType.OUTPUT,
    bundling: {
      image: lambda.Runtime.PYTHON_3_12.bundlingImage,
      command: [
        'bash',
        '-c',
        [
          `pip install --no-cache-dir -r /asset-input/${requirementsFile} -t /asset-output`,
          copyCmd,
        ].join(' && '),
      ],
      local: makeLocalBundler(requirementsFile, sourceStaged),
    },
  });
}

export class EinkgenLambdas extends Construct {
  public readonly generator: lambda.Function;
  public readonly readApi: lambda.Function;
  public readonly deviceStatus: lambda.Function;
  public readonly adminApi: lambda.Function;
  public readonly readApiUrl: string;
  public readonly deviceStatusUrl: string;
  public readonly adminApiUrl: string;
  public readonly adminApiHttp: HttpApi;

  constructor(scope: Construct, id: string, props: EinkgenLambdasProps) {
    super(scope, id);

    const sourceStaged = stageSource();

    // ---- generator ----------------------------------------------------
    this.generator = new lambda.Function(this, 'Generator', {
      functionName: 'einkgen-generator',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'einkgen.lambdas.generator.handler',
      code: bundlePython('requirements-generator.txt', sourceStaged),
      memorySize: 1024,
      timeout: Duration.minutes(5),
      // ARCHITECTURE §4: reserved concurrency = 1 keeps queue drains FIFO-serial.
      reservedConcurrentExecutions: 1,
      logRetention: logs.RetentionDays.TWO_WEEKS,
      environment: {
        EINKGEN_BUCKET: props.bucket.bucketName,
        EINKGEN_CDN_BASE: props.cdnBase,
        EINKGEN_CF_DISTRIBUTION_ID: props.distribution.distributionId,
        OPENAI_API_KEY_SECRET_NAME: props.openaiApiKey.secretName,
        EINKGEN_POLL_INTERVAL_SECONDS: `${props.pollIntervalSeconds}`,
      },
    });
    // ARCHITECTURE §8 access table — generator writes current/ and history/,
    // reads + finalizes queue/, and reads + cleans queue/staged/. Avoid
    // grantReadWrite on the whole bucket: it would also cover web/, firmware/,
    // status/ — the invariant from ARCHITECTURE §12 is that the generator never
    // touches those.
    this.generator.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject', 's3:PutObject', 's3:DeleteObject'],
        resources: [
          `${props.bucket.bucketArn}/current/*`,
          `${props.bucket.bucketArn}/history/*`,
          `${props.bucket.bucketArn}/queue/*`,
        ],
      }),
    );
    this.generator.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:ListBucket'],
        resources: [props.bucket.bucketArn],
      }),
    );
    // Random-pick library: read-only on the cron path. The Lambda calls
    // `s3.head_object` first, which needs ListBucket scoped to `config/*`
    // so a missing file returns 404 (→ fallback to DEFAULTS) instead of 403.
    this.generator.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject'],
        resources: [`${props.bucket.bucketArn}/config/prompt_library.txt`],
      }),
    );
    props.openaiApiKey.grantRead(this.generator);
    this.generator.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['cloudfront:CreateInvalidation'],
        resources: [
          `arn:aws:cloudfront::${props.bucket.stack.account}:distribution/${props.distribution.distributionId}`,
        ],
      }),
    );

    // Async-invoke retries reprocess from scratch — each retry runs another
    // OpenAI generation. PLAN §3 defers a cost cap; this is the cheap
    // pre-emptive bound. Items lost on transient failure can be re-enqueued
    // by the operator.
    new lambda.EventInvokeConfig(this, 'GeneratorInvokeConfig', {
      function: this.generator,
      retryAttempts: 0,
      maxEventAge: Duration.hours(1),
    });

    // No S3 ObjectCreated trigger any more. The queue is now a curated
    // buffer (operator can reorder, run-now, drop items); items sit on
    // the queue until a cron tick or admin "Run" / "Now" explicitly
    // renders the head. See `src/einkgen/lambdas/generator.py` for the
    // full design.

    // EventBridge cron — fires the generator at the cadence configured
    // by ``einkgenPollIntervalSeconds`` in cdk.json (default: 1800 s =
    // 30 min). Each tick (1) tops the queue up to 5 items by expanding
    // random topics from the prompt library via the text LLM, then
    // (2) renders the current head. Event payload sets source=aws.events
    // which generator.py uses to branch. retryAttempts=0 on the target
    // matches the Lambda's async-invoke config — see comment above on
    // cost amplification.
    //
    // Cost guide at gpt-image-2 medium pricing (~$0.04/render): 15 min
    // ≈ $115/mo, 30 min ≈ $55/mo, 60 min ≈ $30/mo. The text-LLM
    // top-up calls are a rounding error. Battery life on the Inkplate
    // scales inversely (15 min → ~10–12 weeks, 30 min → ~3–4 months,
    // 1 h → ~6–9 months on a 3 Ah cell).
    //
    // This rate AND the EINKGEN_POLL_INTERVAL_SECONDS env var above
    // are both driven by the same `pollIntervalSeconds` prop so they
    // can never drift — no point polling more often than cron renders,
    // or rendering more often than the device picks up.
    new events.Rule(this, 'GeneratorCron', {
      // Static name (no cadence embedded) so changing the interval
      // doesn't churn the CloudFormation logical id every deploy.
      ruleName: 'einkgen-generator-cron',
      schedule: events.Schedule.rate(Duration.seconds(props.pollIntervalSeconds)),
      targets: [
        new targets.LambdaFunction(this.generator, {
          retryAttempts: 0,
          maxEventAge: Duration.hours(1),
        }),
      ],
    });

    // ---- read-api -----------------------------------------------------
    this.readApi = new lambda.Function(this, 'ReadApi', {
      functionName: 'einkgen-read-api',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'einkgen.lambdas.read_api.handler',
      code: bundlePython('requirements-read-api.txt', sourceStaged),
      memorySize: 256,
      timeout: Duration.seconds(10),
      logRetention: logs.RetentionDays.TWO_WEEKS,
      environment: {
        EINKGEN_BUCKET: props.bucket.bucketName,
      },
    });
    props.bucket.grantRead(this.readApi);

    // API Gateway HTTP API in front of read-api. Lambda Function URLs with
    // AuthType=NONE are blocked by an AWS account-level public-access setting
    // we can't easily disable; HTTP API public endpoints are not subject to
    // that block. The route is a catch-all GET — the handler dispatches
    // /queue, /history, /status internally based on rawPath, same as it did
    // under the Function URL.
    // CORS allowOrigins:
    //  - props.cdnBase is the operator-facing URL (custom site domain when
    //    configured, the *.cloudfront.net default otherwise).
    //  - The *.cloudfront.net default is always included so direct testing
    //    via the CDN URL keeps working after a custom domain is wired up.
    //  - localhost:5173 for `vite dev`.
    const cloudfrontDefaultOrigin = `https://${props.distribution.distributionDomainName}`;
    const corsOrigins = Array.from(
      new Set([props.cdnBase, cloudfrontDefaultOrigin, 'http://localhost:5173']),
    );
    const readApiHttp = new HttpApi(this, 'ReadApiHttp', {
      apiName: 'einkgen-read-api',
      corsPreflight: {
        allowOrigins: corsOrigins,
        allowMethods: [CorsHttpMethod.GET],
        allowHeaders: ['*'],
        maxAge: Duration.minutes(10),
      },
    });
    readApiHttp.addRoutes({
      path: '/{proxy+}',
      methods: [HttpMethod.GET],
      integration: new HttpLambdaIntegration('ReadApiIntegration', this.readApi),
    });
    this.readApiUrl = readApiHttp.apiEndpoint;

    // ---- device-status ------------------------------------------------
    this.deviceStatus = new lambda.Function(this, 'DeviceStatus', {
      functionName: 'einkgen-device-status',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'einkgen.lambdas.device_status.handler',
      code: bundlePython('requirements-device-status.txt', sourceStaged),
      memorySize: 256,
      timeout: Duration.seconds(10),
      // ARCHITECTURE §12: cap blast radius for token-spam attacks.
      reservedConcurrentExecutions: 5,
      logRetention: logs.RetentionDays.TWO_WEEKS,
      environment: {
        EINKGEN_BUCKET: props.bucket.bucketName,
        DEVICE_STATUS_SECRET_NAME: props.deviceStatusToken.secretName,
      },
    });
    this.deviceStatus.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:PutObject'],
        resources: [`${props.bucket.bucketArn}/status/*`],
      }),
    );
    props.deviceStatusToken.grantRead(this.deviceStatus);

    // API Gateway HTTP API in front of device-status. No CORS — only firmware
    // POSTs here. The route is `POST /` because the firmware uses the base
    // URL (no path suffix) and the handler doesn't dispatch by path.
    const deviceStatusHttp = new HttpApi(this, 'DeviceStatusHttp', {
      apiName: 'einkgen-device-status',
    });
    deviceStatusHttp.addRoutes({
      path: '/',
      methods: [HttpMethod.POST],
      integration: new HttpLambdaIntegration(
        'DeviceStatusIntegration',
        this.deviceStatus,
      ),
    });
    this.deviceStatusUrl = deviceStatusHttp.apiEndpoint;

    // ---- admin-api ----------------------------------------------------
    // Operator-facing write endpoints fronted by /admin/* on the CloudFront
    // distribution (same origin as the SPA → SameSite=Lax cookies just work,
    // and no CORS preflight is needed). The CloudFront behavior is added
    // back in einkgen-stack.ts because the CDN construct doesn't know about
    // this HTTP API at construction time.
    this.adminApi = new lambda.Function(this, 'AdminApi', {
      functionName: 'einkgen-admin-api',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'einkgen.lambdas.admin_api.handler',
      code: bundlePython('requirements-admin-api.txt', sourceStaged),
      memorySize: 256,
      // Larger than read-api because /admin/queue/image base64-decodes up to
      // ~8 MB and writes it to S3 in one shot.
      timeout: Duration.seconds(20),
      // Cap blast radius if a token is ever leaked: even with perfect creds,
      // attacker can't drive more than 5 concurrent generations (the
      // generator's own reservedConcurrentExecutions=1 then serialises them).
      reservedConcurrentExecutions: 5,
      logRetention: logs.RetentionDays.TWO_WEEKS,
      environment: {
        EINKGEN_BUCKET: props.bucket.bucketName,
        EINKGEN_CDN_BASE: props.cdnBase,
        EINKGEN_CF_DISTRIBUTION_ID: props.distribution.distributionId,
        ADMIN_PASSWORD_SECRET_NAME: props.adminPassword.secretName,
        ADMIN_COOKIE_KEY_SECRET_NAME: props.adminCookieSigningKey.secretName,
        // Used by the at="now" enqueue path and POST /admin/queue/<id>/run
        // to async-invoke the generator (InvocationType=Event). Without
        // this the routes still enqueue the item but skip the immediate
        // render; the next cron tick picks it up.
        EINKGEN_GENERATOR_FUNCTION_NAME: this.generator.functionName,
      },
    });
    // Mirror the generator's queue/* access — admin-api writes the
    // staged image, the queue item, and also rewrites position on
    // move-to-top and deletes on cancel.
    this.adminApi.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          's3:PutObject',
          's3:GetObject',
          's3:DeleteObject',
        ],
        resources: [`${props.bucket.bucketArn}/queue/*`],
      }),
    );
    // ListBucket on queue/* so admin can enumerate keys when looking up
    // an item by id (for move-to-top and the per-id /run, DELETE paths).
    this.adminApi.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:ListBucket'],
        resources: [props.bucket.bucketArn],
        conditions: {
          StringLike: {
            's3:prefix': ['queue/*'],
          },
        },
      }),
    );
    // /admin/queue/.../{run,now} fires an async invoke at the generator
    // so the rendering happens off the request path. Scope to the one
    // function — there are no other generators we'd want to call.
    this.generator.grantInvoke(this.adminApi);
    // Random-pick library: full read/write on the single config file so the
    // operator can edit the bank from the SPA Admin tab.
    this.adminApi.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject', 's3:PutObject'],
        resources: [`${props.bucket.bucketArn}/config/prompt_library.txt`],
      }),
    );
    // ListBucket on config/* so head_object on a missing file returns a
    // clean 404 (→ fallback to DEFAULTS) instead of 403. Mirrors the
    // inbound-email pattern.
    this.adminApi.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:ListBucket'],
        resources: [props.bucket.bucketArn],
        conditions: {
          StringLike: {
            's3:prefix': ['config/*'],
          },
        },
      }),
    );
    // /admin/show re-publishes an existing history frame as current — it
    // reads history/<id>/manifest.json and writes current/manifest.json.
    // No new path can be created by the operator: the history id must
    // already exist (or the route 404s), and the bytes are not copied.
    // Per ARCHITECTURE §12, the manifest-tampering mitigation is "only the
    // generator writes current/*". The admin Lambda is operator-trusted
    // (password + HMAC cookie) and already inside that trust boundary, so
    // adding it as a second writer doesn't expand the threat surface.
    this.adminApi.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject'],
        resources: [`${props.bucket.bucketArn}/history/*`],
      }),
    );
    this.adminApi.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject', 's3:PutObject'],
        resources: [`${props.bucket.bucketArn}/current/*`],
      }),
    );
    this.adminApi.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['cloudfront:CreateInvalidation'],
        resources: [
          `arn:aws:cloudfront::${props.bucket.stack.account}:distribution/${props.distribution.distributionId}`,
        ],
      }),
    );
    props.adminPassword.grantRead(this.adminApi);
    props.adminCookieSigningKey.grantRead(this.adminApi);

    // HTTP API. No CORS preflight here — CloudFront fronts the API at the
    // same origin as the SPA, so the browser never issues an OPTIONS request.
    this.adminApiHttp = new HttpApi(this, 'AdminApiHttp', {
      apiName: 'einkgen-admin-api',
    });
    this.adminApiHttp.addRoutes({
      path: '/admin/{proxy+}',
      // ANY so the handler can dispatch on its own and we don't have to
      // enumerate every method/path pair at the platform layer.
      methods: [HttpMethod.ANY],
      integration: new HttpLambdaIntegration(
        'AdminApiIntegration',
        this.adminApi,
      ),
    });
    this.adminApiUrl = this.adminApiHttp.apiEndpoint;
  }
}
