import { Construct } from 'constructs';
import * as path from 'path';
import * as fs from 'fs';
import { Duration } from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3n from 'aws-cdk-lib/aws-s3-notifications';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';

export interface EinkgenLambdasProps {
  bucket: s3.Bucket;
  distribution: cloudfront.Distribution;
  cdnBase: string;
  openaiApiKey: secretsmanager.Secret;
  deviceStatusToken: secretsmanager.Secret;
  pillowLayerArn: string;
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
      return true;
    },
  };
}

function bundlePython(requirementsFile: string, sourceStaged: boolean): lambda.AssetCode {
  const assetRoot = path.join(__dirname, '..', 'lambda');
  // If source isn't staged (synth smoke test), we still need a bundling
  // command that won't crash. The "cp" line is gated with a test.
  const copyCmd = sourceStaged
    ? 'cp -r /asset-input/_src/einkgen /asset-output/einkgen'
    : 'mkdir -p /asset-output/einkgen && echo "stub" > /asset-output/einkgen/__init__.py';
  return lambda.Code.fromAsset(assetRoot, {
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
  public readonly readApiFunctionUrl: lambda.FunctionUrl;
  public readonly deviceStatusFunctionUrl: lambda.FunctionUrl;

  constructor(scope: Construct, id: string, props: EinkgenLambdasProps) {
    super(scope, id);

    const sourceStaged = stageSource();

    const pillowLayer = lambda.LayerVersion.fromLayerVersionArn(
      this,
      'PillowLayer',
      props.pillowLayerArn,
    );

    // ---- generator ----------------------------------------------------
    this.generator = new lambda.Function(this, 'Generator', {
      functionName: 'einkgen-generator',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.X86_64,
      handler: 'einkgen.lambdas.generator.handler',
      code: bundlePython('requirements-generator.txt', sourceStaged),
      memorySize: 1024,
      timeout: Duration.minutes(5),
      // README §4: reserved concurrency = 1 keeps queue drains FIFO-serial.
      reservedConcurrentExecutions: 1,
      layers: [pillowLayer],
      logRetention: logs.RetentionDays.TWO_WEEKS,
      environment: {
        EINKGEN_BUCKET: props.bucket.bucketName,
        EINKGEN_CDN_BASE: props.cdnBase,
        EINKGEN_CF_DISTRIBUTION_ID: props.distribution.distributionId,
        OPENAI_API_KEY_SECRET_NAME: props.openaiApiKey.secretName,
      },
    });
    props.bucket.grantReadWrite(this.generator);
    props.openaiApiKey.grantRead(this.generator);
    this.generator.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['cloudfront:CreateInvalidation'],
        resources: [
          `arn:aws:cloudfront::${props.bucket.stack.account}:distribution/${props.distribution.distributionId}`,
        ],
      }),
    );

    // S3 ObjectCreated trigger. S3 notification filters support exactly one
    // (prefix, suffix) pair per rule — prefix='queue/' + suffix='.json'
    // excludes queue/staged/<hash>.jpg|png cleanly, even though those keys
    // also begin with "queue/". Multi-filter combinations aren't needed.
    props.bucket.addEventNotification(
      s3.EventType.OBJECT_CREATED,
      new s3n.LambdaDestination(this.generator),
      { prefix: 'queue/', suffix: '.json' },
    );

    // EventBridge cron — rate(2 hours). The cron event payload sets
    // source=aws.events which generator.py uses to branch.
    new events.Rule(this, 'GeneratorCron', {
      ruleName: 'einkgen-generator-2h',
      schedule: events.Schedule.rate(Duration.hours(2)),
      targets: [new targets.LambdaFunction(this.generator)],
    });

    // ---- read-api -----------------------------------------------------
    this.readApi = new lambda.Function(this, 'ReadApi', {
      functionName: 'einkgen-read-api',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.X86_64,
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

    // Read API Function URL is its own endpoint (not behind CloudFront), so
    // pinning allowedOrigins to the CF domain is safe: no circular dep.
    this.readApiFunctionUrl = this.readApi.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.NONE,
      cors: {
        allowedOrigins: [`https://${props.distribution.distributionDomainName}`],
        allowedMethods: [lambda.HttpMethod.GET],
        allowedHeaders: ['*'],
        maxAge: Duration.minutes(10),
      },
    });

    // ---- device-status ------------------------------------------------
    this.deviceStatus = new lambda.Function(this, 'DeviceStatus', {
      functionName: 'einkgen-device-status',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.X86_64,
      handler: 'einkgen.lambdas.device_status.handler',
      code: bundlePython('requirements-device-status.txt', sourceStaged),
      memorySize: 256,
      timeout: Duration.seconds(10),
      // README §16: cap blast radius for token-spam attacks.
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

    this.deviceStatusFunctionUrl = this.deviceStatus.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.NONE,
      cors: {
        // Only firmware POSTs here — no browser involvement. Wildcard is fine.
        allowedOrigins: ['*'],
        allowedMethods: [lambda.HttpMethod.POST],
        allowedHeaders: ['*'],
      },
    });
  }
}
