import type {APIRoute} from 'astro';
import {backendURL} from '../../lib/api';
export const GET: APIRoute = async ({params, url}) => {
  const path = params.path || '';
  if (!/^(?:v2\/(?:packages(?::batchGet|\/[^/]+(?:\/(?:activity|commits))?)?|tracks\/[^/]+|targets|status|export)|ui\/(packages(?:\/[^/]+(?:\/(?:activity|commits))?)?|theme))$/.test(path)) {
    return Response.json({detail: 'Not found'}, {status: 404});
  }
  try {
    const response = await fetch(backendURL('/api/' + path + url.search), {signal: AbortSignal.timeout(15000), redirect: 'error'});
    const headers = new Headers({'Content-Type': 'application/json; charset=utf-8'});
    const disposition = response.headers.get('Content-Disposition');
    if (disposition) headers.set('Content-Disposition', disposition);
    return new Response(response.body, {status: response.status, headers});
  } catch { return Response.json({detail: 'Snapshot unavailable'}, {status: 503}); }
};
