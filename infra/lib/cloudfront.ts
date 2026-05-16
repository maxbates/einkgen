import { Construct } from 'constructs';
import { Duration } from 'aws-cdk-lib';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as targets from 'aws-cdk-lib/aws-route53-targets';

export interface EinkgenCdnProps {
  bucket: s3.Bucket;
  /**
   * Optional custom domain to host the site at, e.g. ``einkgen.link``.
   * Requires ``hostedZone``. When set, an ACM cert is issued (DNS-validated
   * against the zone), added as the distribution's alternate domain name,
   * and ALIAS records for the apex (A + AAAA) point at the distribution.
   * If unset, the site stays at the default ``*.cloudfront.net`` URL.
   */
  siteDomain?: string;
  /**
   * Route 53 hosted zone for ``siteDomain``. Used for cert validation and
   * to create the alias records. Required when ``siteDomain`` is set.
   * Typically the same zone we delegate to for inbound email.
   */
  hostedZone?: route53.IHostedZone;
}

export class EinkgenCdn extends Construct {
  public readonly distribution: cloudfront.Distribution;

  constructor(scope: Construct, id: string, props: EinkgenCdnProps) {
    super(scope, id);

    // SPA fallback: rewrite "/" (bare CloudFront domain) and "/web/<sub>/"
    // paths without a file extension to "/web/index.html". The Inkplate
    // device path "/current/..." is a different behavior, so this function
    // only runs for the default behavior.
    const spaRewrite = new cloudfront.Function(this, 'SpaRewriteFn', {
      functionName: `einkgen-spa-rewrite`,
      runtime: cloudfront.FunctionRuntime.JS_2_0,
      code: cloudfront.FunctionCode.fromInline(`
function handler(event) {
  var req = event.request;
  var uri = req.uri;
  if (uri === '/' || uri === '/web' || uri === '/web/') {
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

    // history/* gate: only processed.bmp is publicly served via the CDN.
    // history/<id>/original.png is the raw user upload and history/<id>/
    // manifest.json is read by the read-api Lambda over IAM (see ARCHITECTURE §8
    // access policy). Both must NOT be reachable from the public CDN.
    const historyFilter = new cloudfront.Function(this, 'HistoryFilterFn', {
      functionName: `einkgen-history-filter`,
      runtime: cloudfront.FunctionRuntime.JS_2_0,
      code: cloudfront.FunctionCode.fromInline(`
function handler(event) {
  var req = event.request;
  var uri = req.uri;
  if (uri.indexOf('/history/') === 0 && uri.indexOf('/processed.bmp') !== uri.length - '/processed.bmp'.length) {
    return { statusCode: 403, statusDescription: 'Forbidden' };
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

    // Optional custom-domain wiring. We create the cert + alias records
    // inside the construct so the stack only needs to pass one flag pair.
    // ACM certs for CloudFront MUST be in us-east-1; this stack already
    // deploys there, so a plain ``acm.Certificate`` works.
    let certificate: acm.ICertificate | undefined;
    const domainNames: string[] = [];
    if (props.siteDomain && props.hostedZone) {
      certificate = new acm.Certificate(this, 'SiteCertificate', {
        domainName: props.siteDomain,
        // SAN so ``www.<domain>`` and arbitrary other subdomains work too
        // without an extra cert later. DNS-validated against our hosted zone.
        subjectAlternativeNames: [`*.${props.siteDomain}`],
        validation: acm.CertificateValidation.fromDns(props.hostedZone),
      });
      domainNames.push(props.siteDomain);
    } else if (props.siteDomain || props.hostedZone) {
      throw new Error(
        'EinkgenCdn: siteDomain and hostedZone must be provided together',
      );
    }

    this.distribution = new cloudfront.Distribution(this, 'Distribution', {
      comment: 'einkgen — single distribution fronting the bucket',
      defaultRootObject: '',
      ...(domainNames.length > 0 ? { domainNames, certificate } : {}),
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
          functionAssociations: [
            {
              function: historyFilter,
              eventType: cloudfront.FunctionEventType.VIEWER_REQUEST,
            },
          ],
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

    // ALIAS records pointing the apex (and ``www``) at the distribution.
    // Apex must use ALIAS rather than CNAME because RFC 1034 forbids
    // CNAME-at-apex; Route 53's alias is the AWS-specific solution that
    // resolves to the distribution's IPs at query time.
    if (props.siteDomain && props.hostedZone) {
      const aliasTarget = route53.RecordTarget.fromAlias(
        new targets.CloudFrontTarget(this.distribution),
      );
      new route53.ARecord(this, 'SiteAliasA', {
        zone: props.hostedZone,
        recordName: props.siteDomain,
        target: aliasTarget,
      });
      new route53.AaaaRecord(this, 'SiteAliasAAAA', {
        zone: props.hostedZone,
        recordName: props.siteDomain,
        target: aliasTarget,
      });
    }
  }
}
