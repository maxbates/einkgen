import { Construct } from 'constructs';
import { Duration } from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as cwactions from 'aws-cdk-lib/aws-cloudwatch-actions';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as subs from 'aws-cdk-lib/aws-sns-subscriptions';

export interface EinkgenObservabilityProps {
  envName: string;
  generator: lambda.Function;
  readApi: lambda.Function;
  deviceStatus: lambda.Function;
  adminApi: lambda.Function;
  /**
   * Threshold for the daily generator-invocation alarm. Each invocation
   * is roughly one ``gpt-image-2`` medium-quality call (~$0.04). Default
   * 100/day ≈ ~$4/day. See `einkgenDailyRenderCap` in cdk.json.
   */
  dailyRenderCap: number;
  /**
   * Optional email address to subscribe to the alarm SNS topic. When
   * unset, the topic is still created and the alarm still fires, but
   * nothing is subscribed (operator can attach a subscription manually
   * via the console). See `einkgenAlarmEmail` in cdk.json.
   */
  alarmEmail?: string;
}

const METRIC_NAMESPACE = 'einkgen';

export class EinkgenObservability extends Construct {
  public readonly dashboard: cloudwatch.Dashboard;
  public readonly alarmTopic: sns.Topic;
  public readonly dailyRenderAlarm: cloudwatch.Alarm;

  constructor(scope: Construct, id: string, props: EinkgenObservabilityProps) {
    super(scope, id);

    const fns = [
      { name: 'generator', fn: props.generator },
      { name: 'read-api', fn: props.readApi },
      { name: 'device-status', fn: props.deviceStatus },
      { name: 'admin-api', fn: props.adminApi },
    ];

    // Metric filter per log group on the literal token ERROR. Using
    // `fn.logGroup` rather than `fromLogGroupName(...)` so CFN tracks the
    // dependency on the LogRetention custom resource — otherwise the filter
    // can be created before the log group exists and PutMetricFilter fails
    // at deploy time.
    // Per-Lambda metric names rather than a shared name + Lambda dimension:
    // CloudWatch's literal-token filter pattern can't populate dimensions
    // (the pattern extracts no structured values), and dimensions also
    // mutually exclude `defaultValue`. Distinct metric names sidesteps both.
    const errorMetrics: cloudwatch.IMetric[] = [];
    for (const { name, fn } of fns) {
      const metricName = `ErrorLogCount-${name}`;
      new logs.MetricFilter(this, `ErrorFilter-${name}`, {
        logGroup: fn.logGroup,
        filterPattern: logs.FilterPattern.literal('ERROR'),
        metricNamespace: METRIC_NAMESPACE,
        metricName,
        metricValue: '1',
        defaultValue: 0,
      });
      errorMetrics.push(
        new cloudwatch.Metric({
          namespace: METRIC_NAMESPACE,
          metricName,
          statistic: cloudwatch.Stats.SUM,
          period: Duration.minutes(5),
          label: name,
        }),
      );
    }

    this.dashboard = new cloudwatch.Dashboard(this, 'Dashboard', {
      dashboardName: `einkgen-${props.envName}`,
    });

    for (const { name, fn } of fns) {
      this.dashboard.addWidgets(
        new cloudwatch.GraphWidget({
          title: `${name} — invocations & errors`,
          left: [fn.metricInvocations({ period: Duration.minutes(5) })],
          right: [fn.metricErrors({ period: Duration.minutes(5) })],
          width: 12,
        }),
        new cloudwatch.GraphWidget({
          title: `${name} — duration p50 / p99`,
          left: [
            fn.metricDuration({ statistic: 'p50', period: Duration.minutes(5), label: 'p50' }),
            fn.metricDuration({ statistic: 'p99', period: Duration.minutes(5), label: 'p99' }),
          ],
          width: 12,
        }),
      );
    }

    this.dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'ERROR log counts (all Lambdas)',
        left: errorMetrics,
        width: 24,
      }),
    );

    // ---- Daily generator-invocation alarm ------------------------------
    // The generator Lambda is the only OpenAI spend trigger. Every
    // invocation is roughly one `gpt-image-2` medium-quality call
    // (~$0.04). Alarm on cumulative invocations over a rolling 24 h
    // window so a leaked device token or misconfigured cron can't
    // drain the OpenAI budget overnight without an operator signal.
    //
    // Threshold and (optional) email subscriber come from CDK context
    // flags wired in einkgen-stack.ts.
    this.alarmTopic = new sns.Topic(this, 'AlarmTopic', {
      displayName: `einkgen ${props.envName} alarms`,
    });
    if (props.alarmEmail) {
      this.alarmTopic.addSubscription(new subs.EmailSubscription(props.alarmEmail));
    }

    const dailyInvocations = props.generator.metricInvocations({
      // 1-day period so a single datapoint = the 24 h rolling sum.
      period: Duration.days(1),
      statistic: cloudwatch.Stats.SUM,
      label: 'generator invocations (24h)',
    });
    this.dailyRenderAlarm = new cloudwatch.Alarm(this, 'GeneratorDailyRenderCap', {
      alarmName: `einkgen-${props.envName}-generator-daily-render-cap`,
      alarmDescription:
        `einkgen generator invocations exceeded ${props.dailyRenderCap} in a 24h window. ` +
        'At ~$0.04 per gpt-image-2 medium call this is a runaway cost signal — ' +
        'investigate /wake traffic, the device token, and the cron rule.',
      metric: dailyInvocations,
      threshold: props.dailyRenderCap,
      evaluationPeriods: 1,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      // INSUFFICIENT_DATA keeps the alarm quiet during the first 24h
      // post-deploy / after long idle stretches.
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    this.dailyRenderAlarm.addAlarmAction(new cwactions.SnsAction(this.alarmTopic));

    this.dashboard.addWidgets(
      new cloudwatch.AlarmWidget({
        title: `generator daily render cap (≤ ${props.dailyRenderCap} / 24h)`,
        alarm: this.dailyRenderAlarm,
        width: 24,
      }),
    );

    // ---- Empty-generated-queue alarm -----------------------------------
    // The generator logs ``BUFFER_EMPTY_AFTER_REFILL`` at the end of any
    // cron tick that finishes with a generated-queue depth of 0. That
    // means cron tried to refill the buffer and couldn't — typically
    // because the prompt library was emptied AND ``expand_topic`` is
    // failing (text-LLM outage, key revoked) so the raw-topic fallback
    // also can't enqueue anything. Without an alert the device just
    // keeps drawing the same frame indefinitely after the last
    // pre-rendered marker is popped.
    //
    // Two consecutive empties (at the default 30-min cadence ≈ 1 h)
    // page the operator via the shared alarm SNS topic.
    const bufferEmptyMetric = 'GeneratedQueueEmptyTicks';
    new logs.MetricFilter(this, 'BufferEmptyFilter', {
      logGroup: props.generator.logGroup,
      filterPattern: logs.FilterPattern.literal('BUFFER_EMPTY_AFTER_REFILL'),
      metricNamespace: METRIC_NAMESPACE,
      metricName: bufferEmptyMetric,
      metricValue: '1',
      defaultValue: 0,
    });
    const bufferEmptyAlarm = new cloudwatch.Alarm(this, 'GeneratedQueueEmptyAlarm', {
      alarmName: `einkgen-${props.envName}-generated-queue-empty`,
      alarmDescription:
        'einkgen generator finished two consecutive cron ticks with the ' +
        'generated-queue (pre-rendered buffer) at 0. Once the device pops ' +
        'the last marker it will redraw the same frame indefinitely. ' +
        'Check: prompt library has topics; OpenAI text + image keys still ' +
        'valid; no recurring PermanentItemError in generator logs.',
      metric: new cloudwatch.Metric({
        namespace: METRIC_NAMESPACE,
        metricName: bufferEmptyMetric,
        statistic: cloudwatch.Stats.SUM,
        period: Duration.minutes(30),
        label: 'buffer-empty ticks',
      }),
      threshold: 0,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
    bufferEmptyAlarm.addAlarmAction(new cwactions.SnsAction(this.alarmTopic));

    this.dashboard.addWidgets(
      new cloudwatch.AlarmWidget({
        title: 'generated-queue empty (≥2 consecutive cron ticks)',
        alarm: bufferEmptyAlarm,
        width: 24,
      }),
    );
  }
}
