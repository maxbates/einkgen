import { App, Stack } from 'aws-cdk-lib';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as lambda from 'aws-cdk-lib/aws-lambda';

import { EinkgenObservability } from '../lib/observability';

// Tiny test stack: four stub Lambdas + the observability construct. We
// snapshot just the SNS topic + alarm resources rather than the whole
// stack — the dashboard JSON is large and noisy, and Lambda asset hashes
// would force frequent re-snapshotting.

function buildStack(opts: { dailyRenderCap?: number; alarmEmail?: string } = {}) {
  const app = new App();
  const stack = new Stack(app, 'TestStack');
  const stub = (id: string) =>
    new lambda.Function(stack, id, {
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'index.handler',
      code: lambda.Code.fromInline('def handler(event, context):\n    return {}\n'),
      functionName: `einkgen-test-${id.toLowerCase()}`,
    });

  new EinkgenObservability(stack, 'Observability', {
    envName: 'test',
    generator: stub('Generator'),
    readApi: stub('ReadApi'),
    deviceStatus: stub('DeviceStatus'),
    adminApi: stub('AdminApi'),
    dailyRenderCap: opts.dailyRenderCap ?? 100,
    ...(opts.alarmEmail ? { alarmEmail: opts.alarmEmail } : {}),
  });

  return Template.fromStack(stack);
}

describe('EinkgenObservability — daily render cap', () => {
  it('creates an SNS topic and a CloudWatch alarm wired to it', () => {
    const t = buildStack();

    t.resourceCountIs('AWS::SNS::Topic', 1);
    t.hasResourceProperties('AWS::SNS::Topic', {
      DisplayName: 'einkgen test alarms',
    });

    t.resourceCountIs('AWS::CloudWatch::Alarm', 1);
    t.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: 'einkgen-test-generator-daily-render-cap',
      ComparisonOperator: 'GreaterThanThreshold',
      EvaluationPeriods: 1,
      Threshold: 100,
      TreatMissingData: 'notBreaching',
      AlarmActions: Match.arrayWith([
        Match.objectLike({ Ref: Match.stringLikeRegexp('^ObservabilityAlarmTopic') }),
      ]),
      Metrics: Match.arrayWith([
        Match.objectLike({
          MetricStat: Match.objectLike({
            Metric: Match.objectLike({
              Namespace: 'AWS/Lambda',
              MetricName: 'Invocations',
              Dimensions: Match.arrayWith([
                Match.objectLike({ Name: 'FunctionName' }),
              ]),
            }),
            Period: 86400,
            Stat: 'Sum',
          }),
        }),
      ]),
    });
  });

  it('subscribes the alarm email when set', () => {
    const t = buildStack({ alarmEmail: 'ops@example.com' });

    t.resourceCountIs('AWS::SNS::Subscription', 1);
    t.hasResourceProperties('AWS::SNS::Subscription', {
      Protocol: 'email',
      Endpoint: 'ops@example.com',
    });
  });

  it('does not subscribe anything when alarm email is unset', () => {
    const t = buildStack();
    t.resourceCountIs('AWS::SNS::Subscription', 0);
  });

  it('respects a custom dailyRenderCap threshold', () => {
    const t = buildStack({ dailyRenderCap: 250 });
    t.hasResourceProperties('AWS::CloudWatch::Alarm', { Threshold: 250 });
  });

  it('snapshot — alarm + topic resources stay stable across changes', () => {
    const t = buildStack({ alarmEmail: 'ops@example.com' });
    expect({
      topics: t.findResources('AWS::SNS::Topic'),
      subscriptions: t.findResources('AWS::SNS::Subscription'),
      alarms: t.findResources('AWS::CloudWatch::Alarm'),
    }).toMatchSnapshot();
  });
});
