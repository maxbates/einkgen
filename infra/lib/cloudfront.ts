import { Construct } from 'constructs';
import { Duration } from 'aws-cdk-lib';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as s3 from 'aws-cdk-lib/aws-s3';

export interface EinkgenCdnProps {
  bucket: s3.Bucket;
}

export class EinkgenCdn extends Construct {
  public readonly distribution: cloudfront.Distribution;

  constructor(scope: Construct, id: string, props: EinkgenCdnProps) {
    super(scope, id);

    // SPA fallback: rewrite "/web/" (and any "/web/<sub>/" path without a file
    // extension) to "/web/index.html". The Inkplate device path "/current/..."
    // is a different behavior, so this function only runs for the default
    // /web/* behavior.
    const spaRewrite = new cloudfront.Function(this, 'SpaRewriteFn', {
      functionName: `einkgen-spa-rewrite`,
      runtime: cloudfront.FunctionRuntime.JS_2_0,
      code: cloudfront.FunctionCode.fromInline(`
function handler(event) {
  var req = event.request;
  var uri = req.uri;
  if (uri === '/web' || uri === '/web/') {
    req.uri = '/web/index.html';
    return req;
  }
  if (uri.indexOf('/web/') === 0) {
    var last = uri.substring(uri.lastIndexOf('/') + 1);
    if (last.indexOf('.') === -1) {
      req.uri = '/web/index.html';
    }
  }
  return req;
}
      `),
    });

    // S3 origin with OAC. Modern (post Aug 2022) replacement for OAI;
    // bucket policy gets attached automatically by the construct.
    const s3Origin = origins.S3BucketOrigin.withOriginAccessControl(props.bucket);

    const currentCachePolicy = new cloudfront.CachePolicy(this, 'CurrentCachePolicy', {
      cachePolicyName: 'einkgen-current-60s',
      defaultTtl: Duration.seconds(60),
      minTtl: Duration.seconds(0),
      maxTtl: Duration.seconds(300),
      enableAcceptEncodingGzip: true,
      enableAcceptEncodingBrotli: true,
      // Device sends If-None-Match against the manifest, so forward ETag-related
      // request headers via the origin (CloudFront passes If-None-Match through
      // automatically as part of cache-key normalization).
    });

    const historyCachePolicy = new cloudfront.CachePolicy(this, 'HistoryCachePolicy', {
      cachePolicyName: 'einkgen-history-1h',
      // history/<id>/* is immutable once written; safe to cache for an hour.
      defaultTtl: Duration.hours(1),
      minTtl: Duration.minutes(5),
      maxTtl: Duration.days(1),
      enableAcceptEncodingGzip: true,
      enableAcceptEncodingBrotli: true,
    });

    const stagedCachePolicy = new cloudfront.CachePolicy(this, 'StagedCachePolicy', {
      cachePolicyName: 'einkgen-staged-1h',
      // Queue tab thumbnails: object key includes a content hash so caching is safe.
      defaultTtl: Duration.hours(1),
      minTtl: Duration.minutes(5),
      maxTtl: Duration.days(1),
      enableAcceptEncodingGzip: true,
      enableAcceptEncodingBrotli: true,
    });

    this.distribution = new cloudfront.Distribution(this, 'Distribution', {
      comment: 'einkgen — single distribution fronting the bucket',
      defaultRootObject: '',
      defaultBehavior: {
        origin: s3Origin,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        functionAssociations: [
          {
            function: spaRewrite,
            eventType: cloudfront.FunctionEventType.VIEWER_REQUEST,
          },
        ],
      },
      additionalBehaviors: {
        'current/*': {
          origin: s3Origin,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          cachePolicy: currentCachePolicy,
        },
        'history/*': {
          origin: s3Origin,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          cachePolicy: historyCachePolicy,
        },
        'queue/staged/*': {
          origin: s3Origin,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
          cachePolicy: stagedCachePolicy,
        },
      },
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
    });
  }
}
