import test from 'node:test';
import assert from 'node:assert/strict';
import {createHmac} from 'node:crypto';
import {Vidgen, VidgenError} from './dist/index.js';
const json = (body, status = 200, headers = {}) => new Response(JSON.stringify(body), {status, headers});

test('safe retries preserve the body and idempotency key; errors expose request ID', async () => {
  const calls = [], waits = [];
  const client = new Vidgen('https://vidgen.example', 'secret', 'project', async (url, options) => {
    calls.push(options);
    return calls.length === 1 ? json({error: {code: 'rate_limited', message: 'slow', request_id: 'request'}}, 429, {'Retry-After': '2'}) : json({id: 'job', status: 'queued'});
  }, {sleep: async ms => waits.push(ms)});
  assert.equal((await client.create({prompt: 'Explain inventory'})).id, 'job');
  assert.equal(calls[0].headers['Idempotency-Key'], calls[1].headers['Idempotency-Key']);
  assert.equal(calls[0].body, calls[1].body);
  assert.equal(calls[0].headers['X-Project-ID'], 'project');
  assert.deepEqual(waits, [2000]);
  const error = new VidgenError(409, {error: {code: 'idempotency_conflict', message: 'changed body', request_id: 'r'}});
  assert.equal(error.code, 'idempotency_conflict'); assert.equal(error.requestId, 'r');
});

test('unkeyed mutations do not retry, pagination follows every opaque cursor', async () => {
  let calls = 0;
  const broken = new Vidgen('https://vidgen.example', 'key', undefined, async () => { calls++; throw new Error('reset'); });
  await assert.rejects(broken.request('POST', '/v1/videos', {})); assert.equal(calls, 1);
  const urls = [];
  const client = new Vidgen('https://vidgen.example', 'key', undefined, async url => {
    urls.push(url);
    return urls.length === 1 ? json({data: [{id: 'first'}], has_more: true, next_cursor: 'opaque:cursor'}) : json({data: [{id: 'second'}], has_more: false, next_cursor: null});
  });
  const ids = []; for await (const row of client.videos({limit: 1})) ids.push(row.id);
  assert.deepEqual(ids, ['first', 'second']); assert.match(urls[1], /after=opaque%3Acursor/);
});

test('multipart retry reuses bytes; binary downloads and terminal waits work', async () => {
  const bodies = [];
  const client = new Vidgen('https://vidgen.example', 'key', undefined, async (url, options) => {
    if (url.endsWith('/file')) return new Response(new Uint8Array([1, 2, 3]));
    if (url.endsWith('/videos/job')) return json({id: 'job', status: 'needs_input'});
    bodies.push(options.body);
    return bodies.length === 1 ? json({}, 503) : json({id: 'asset'});
  }, {sleep: async () => {}});
  assert.equal((await client.upload(new Blob(['private']), 'facts.txt')).id, 'asset');
  assert.equal(bodies[0], bodies[1]); assert.equal(await bodies[1].get('file').text(), 'private');
  assert.deepEqual([...new Uint8Array(await client.downloadAsset('asset'))], [1, 2, 3]);
  assert.equal((await client.wait('job')).status, 'needs_input');
});

test('webhook signature validates the original bytes and timestamp', async () => {
  const body = new TextEncoder().encode('{"message":"你好"}'), timestamp = Math.floor(Date.now() / 1000);
  const signature = createHmac('sha256', 'secret').update(`${timestamp}.`).update(body).digest('hex');
  assert.equal(await Vidgen.verifyWebhook('secret', `t=${timestamp},v1=${signature}`, body), true);
  assert.equal(await Vidgen.verifyWebhook('wrong', `t=${timestamp},v1=${signature}`, body), false);
  assert.equal(await Vidgen.verifyWebhook('secret', `t=0,v1=${signature}`, body), false);
});

test('there is no price to fetch; budget approval needs no number', async () => {
  const client = new Vidgen('https://vidgen.example', 'key', undefined, async (url, options) => json({id: 'job', status: 'queued', body: options.body}));
  for (const call of [() => client.quote({}), () => client.prices()]) assert.throws(call, error => error instanceof VidgenError && error.status === 403 && error.code === 'contact_sales');
  const resumed = await client.approveBudget('job');
  assert.deepEqual(JSON.parse(resumed.body), {approve_budget: true});
  const confirmed = await client.confirmDuration('job', 75);
  assert.deepEqual(JSON.parse(confirmed.body), {duration_sec: 75});
});
