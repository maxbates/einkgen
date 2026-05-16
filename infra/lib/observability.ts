import { Construct } from 'constructs';
import { Duration } from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';

export interface EinkgenObservabilityProps {
  envName: string;
  generator: lambda.Function;
  readApi: lambda.Function;
  deviceStatus: lambda.Function;
  adminApi: lambda.Function;
}

const METRIC_NAMESPACE = 'einkgen';

export class EinkgenObservability extends Construct {
  public readonly dashboard: cloudwatch.Dashboard;

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
  }
}
