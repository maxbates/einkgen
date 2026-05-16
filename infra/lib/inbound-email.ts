import { Construct } from 'constructs';
import { Duration, Stack } from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3n from 'aws-cdk-lib/aws-s3-notifications';
import * as ses from 'aws-cdk-lib/aws-ses';
import * as sesActions from 'aws-cdk-lib/aws-ses-actions';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as cr from 'aws-cdk-lib/custom-resources';

export interface EinkgenInboundEmailProps {
  bucket: s3.Bucket;
  /**
   * Domain SES receives mail for, e.g. ``submit.example.com``.
   */
  inboundDomain: string;
  /**
   * Route 53 hosted zone for ``inboundDomain``. When provided, CDK
   * auto-creates the DKIM CNAME records (via ``Identity.publicHostedZone``)
   * and the MX record pointing at the SES inbound endpoint. Skip this and
   * the operator must add those records manually post-deploy.
   */
  hostedZone?: route53.IHostedZone;
  /**
   * The From: address used for replies. Must be on ``inboundDomain`` (so the
   * EmailIdentity verifies it implicitly). Defaults to ``einkgen@<domain>``.
   */
  replyFromAddress?: string;
  /**
   * Optional URL included in rejection replies as the "run your own" pointer.
   */
  projectUrl?: string;
  /**
   * Python Lambda asset to use for the handler (built by the parent
   * construct's bundler).
   */
  code: lambda.Code;

  /**
   * Email addresses to seed into ``config/email_allowlist.txt`` on first
   * deploy of the inbound stack. Subsequent deploys do NOT overwrite the
   * file — the operator owns it via the ``einkgen allowlist`` CLI from
   * then on.
   */
  seedAllowlist?: string[];
}

const INBOUND_PREFIX = 'inbound/';

/**
 * SES receives mail at `*@<inboundDomain>`, writes the raw RFC 5322 message
 * to `s3://<bucket>/inbound/`, S3 ObjectCreated triggers the inbound-email
 * Lambda, which parses, allowlists, enqueues, and replies.
 *
 * One-time operator setup after `cdk deploy`:
 *   1. Activate the receipt rule set in the SES console (CDK can't activate
 *      a rule set because only one can be active per account at a time and
 *      we don't want to clobber whatever else the account is doing).
 *   2. If the SES account is still in sandbox, request production access so
 *      replies can be delivered to arbitrary recipients.
 *
 * When ``hostedZone`` is provided, CDK also creates the DKIM CNAMEs and the
 * MX record automatically. Without it, the operator adds those by hand at
 * the registrar / external DNS provider.
 */
export class EinkgenInboundEmail extends Construct {
  public readonly handler: lambda.Function;
  public readonly emailIdentity: ses.EmailIdentity;
  public readonly receiptRuleSet: ses.ReceiptRuleSet;

  constructor(scope: Construct, id: string, props: EinkgenInboundEmailProps) {
    super(scope, id);

    const replyFrom = props.replyFromAddress ?? `einkgen@${props.inboundDomain}`;

    // When a hosted zone is supplied, ``Identity.publicHostedZone`` causes
    // the EmailIdentity construct to publish the three DKIM CNAMEs into
    // the zone automatically. Without it, the operator adds them by hand.
    this.emailIdentity = new ses.EmailIdentity(this, 'Identity', {
      identity: props.hostedZone
        ? ses.Identity.publicHostedZone(props.hostedZone)
        : ses.Identity.domain(props.inboundDomain),
    });

    // MX record so SES becomes the inbound mail server for the domain.
    // SES inbound is only available in us-east-1, us-west-2, eu-west-1;
    // we use the region the stack is deployed into, which the operator
    // confirms in QUICKSTART §1.4.
    if (props.hostedZone) {
      new route53.MxRecord(this, 'InboundMx', {
        zone: props.hostedZone,
        recordName: props.inboundDomain,
        values: [
          {
            priority: 10,
            hostName: `inbound-smtp.${Stack.of(this).region}.amazonaws.com`,
          },
        ],
        ttl: Duration.minutes(5),
      });
    }

    this.handler = new lambda.Function(this, 'Function', {
      functionName: 'einkgen-inbound-email',
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'einkgen.lambdas.inbound_email.handler',
      code: props.code,
      memorySize: 512,
      // Image-edit submissions can chain to a synchronous-feeling generation
      // through the queue, but this Lambda itself only parses + enqueues +
      // replies. 30s leaves plenty of room for the SES SendEmail round trip.
      timeout: Duration.seconds(30),
      // Bound blast radius if an attacker manages to brute-force inbound
      // delivery against the allowlist (unlikely — DKIM gates the auth) or
      // floods the rule with messages. The Lambda is idempotent enough that
      // capping concurrency doesn't risk data loss; S3 events queue.
      reservedConcurrentExecutions: 5,
      logRetention: logs.RetentionDays.TWO_WEEKS,
      environment: {
        EINKGEN_BUCKET: props.bucket.bucketName,
        EINKGEN_INBOUND_PREFIX: INBOUND_PREFIX,
        EINKGEN_REPLY_FROM: replyFrom,
        // INFO-level so allowlist hits, rejections, and enqueue outcomes show
        // up in CloudWatch by default. Errors are noisy enough at WARN/ERROR
        // that the operational story benefits from the extra line per call.
        AWS_LAMBDA_LOG_LEVEL: 'INFO',
        ...(props.projectUrl ? { EINKGEN_PROJECT_URL: props.projectUrl } : {}),
      },
    });

    // Scoped IAM — match the pattern used by generator/device-status: never
    // grantReadWrite on the whole bucket; spell out the prefix-level rights.
    this.handler.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject', 's3:DeleteObject'],
        resources: [`${props.bucket.bucketArn}/${INBOUND_PREFIX}*`],
      }),
    );
    // ListBucket on the bucket: ``s3.head_object`` for the allowlist returns
    // 404 (not 403) when the key is missing, and an S3 retry that races
    // against ``_safe_delete`` returns a clean "object gone" error instead
    // of a misleading "no ListBucket policy" message. Scoped to the prefixes
    // this Lambda actually inspects.
    this.handler.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:ListBucket'],
        resources: [props.bucket.bucketArn],
        conditions: {
          StringLike: {
            's3:prefix': [`${INBOUND_PREFIX}*`, 'config/*'],
          },
        },
      }),
    );
    this.handler.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject'],
        resources: [`${props.bucket.bucketArn}/config/email_allowlist.txt`],
      }),
    );
    this.handler.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:PutObject'],
        resources: [
          `${props.bucket.bucketArn}/queue/*`,
        ],
      }),
    );
    // Replies go through SES. Constraint on FromAddress so a compromised
    // Lambda can't impersonate other addresses on the same SES identity.
    this.handler.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['ses:SendEmail', 'ses:SendRawEmail'],
        resources: ['*'],
        conditions: {
          StringEquals: {
            'ses:FromAddress': replyFrom,
          },
        },
      }),
    );

    // S3 ObjectCreated trigger. Filter to the inbound/ prefix so other
    // writers (queue/, history/, config/) don't fan us out.
    props.bucket.addEventNotification(
      s3.EventType.OBJECT_CREATED,
      new s3n.LambdaDestination(this.handler),
      { prefix: INBOUND_PREFIX },
    );

    // SES receipt rule set. Activation is manual — see the class doc. CDK
    // creates the rule set but leaves the account-level active rule set
    // untouched.
    this.receiptRuleSet = new ses.ReceiptRuleSet(this, 'RuleSet', {
      receiptRuleSetName: 'einkgen-inbound',
    });

    // Grant SES PutObject on inbound/ — required for the S3 action.
    props.bucket.addToResourcePolicy(
      new iam.PolicyStatement({
        principals: [new iam.ServicePrincipal('ses.amazonaws.com')],
        actions: ['s3:PutObject'],
        resources: [`${props.bucket.bucketArn}/${INBOUND_PREFIX}*`],
        conditions: {
          StringEquals: {
            'AWS:SourceAccount': props.bucket.stack.account,
          },
        },
      }),
    );

    // Seed the allowlist on first deploy. AwsCustomResource with onCreate
    // only (no onUpdate / no onDelete) means: this PutObject fires exactly
    // once when CloudFormation first materialises this resource. After
    // that the operator owns the file — `einkgen allowlist add foo@bar`
    // edits won't be overwritten by subsequent `cdk deploy` runs, and
    // `cdk destroy` leaves the file behind.
    if (props.seedAllowlist && props.seedAllowlist.length > 0) {
      // Normalise + dedupe + sort so the seeded file looks identical to
      // one written by ``email_allowlist.write()`` (lowercase, sorted).
      const cleaned = Array.from(
        new Set(
          props.seedAllowlist
            .map((e) => e.trim().toLowerCase())
            .filter((e) => e.length > 0),
        ),
      ).sort();
      const header =
        '# einkgen email allowlist — senders permitted to enqueue via inbound email.\n' +
        '# One address per line. Lines starting with # are ignored.\n' +
        '# Managed by `einkgen allowlist {ls,add,rm}` but free to edit by hand.\n';
      const body = header + cleaned.join('\n') + (cleaned.length ? '\n' : '');

      new cr.AwsCustomResource(this, 'AllowlistSeed', {
        onCreate: {
          service: 'S3',
          action: 'putObject',
          parameters: {
            Bucket: props.bucket.bucketName,
            Key: 'config/email_allowlist.txt',
            Body: body,
            ContentType: 'text/plain; charset=utf-8',
          },
          // Stable physical id so CFN never thinks this needs to re-run.
          physicalResourceId: cr.PhysicalResourceId.of(
            `${props.bucket.bucketName}/config/email_allowlist.txt`,
          ),
        },
        // No onUpdate / onDelete: leave the operator's edits alone, and
        // leave the file behind on stack delete (it's not a load-bearing
        // CFN resource anyway).
        policy: cr.AwsCustomResourcePolicy.fromStatements([
          new iam.PolicyStatement({
            actions: ['s3:PutObject'],
            resources: [`${props.bucket.bucketArn}/config/email_allowlist.txt`],
          }),
        ]),
      });
    }

    this.receiptRuleSet.addRule('CatchAll', {
      // Recipients is a prefix-match list; passing the bare domain matches
      // any address @<domain>.
      recipients: [props.inboundDomain],
      enabled: true,
      scanEnabled: true,
      actions: [
        new sesActions.S3({
          bucket: props.bucket,
          objectKeyPrefix: INBOUND_PREFIX,
        }),
      ],
    });
  }
}
