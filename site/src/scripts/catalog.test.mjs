import { test } from 'node:test';
import assert from 'node:assert/strict';
import { rowHtml, short, sortGames, toGame } from './catalog.ts';

test('catalog entries become rows like docs/GAMES.md', () => {
  assert.equal(toGame({ title: 'x', status: 'maybe' }), null);
  assert.equal(toGame(null), null);
  const g = toGame({ title: 'A', package: 'p', status: 'issues', engine: 'Unity', xr: 'OpenXR', kind: 'rift',
    notes: 'First sentence. Second one.', details: 'More.', tested_version: '1.2', frame: ['frame.x'],
    alt_overport: ['patch_y'], adapter: { focus_hold: 1 }, verified: { date: new Date('2026-10-02T00:00:00Z'), app: '0.9.0' } });
  assert.deepEqual(g, { title: 'A', package: 'p', status: 'issues', platform: 'PC VR', engine: 'Unity', xr: 'OpenXR',
    note: 'First sentence.', checked: '2026-10-02', details: 'First sentence. Second one. More.',
    patches: ['frame.x', 'overport.patch_y', 'adapter.focus_hold'], version: '1.2', app: '0.9.0' });
  assert.equal(toGame({ title: 'B', status: 'works', notes: 'hidden' }).note, '');
});

test('short notes are cut at 160 characters', () => {
  assert.equal(short('x'.repeat(200)).length, 158);
});

test('order: works, issues, unsupported, then title', () => {
  const games = ['unsupported:b', 'works:z', 'issues:a', 'works:a'].map((s) => {
    const [status, title] = s.split(':');
    return toGame({ status, title });
  });
  assert.deepEqual(sortGames(games).map((g) => g.title), ['a', 'z', 'a', 'b']);
});

test('rows escape what comes from GitHub', () => {
  const html = rowHtml(toGame({ title: '<img src=x onerror=alert(1)>', status: 'works', engine: '"x' }));
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('&#60;img'));
  assert.ok(html.includes('data-engine="&#34;x"'));
});
