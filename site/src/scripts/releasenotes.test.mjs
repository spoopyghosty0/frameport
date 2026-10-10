import { test } from 'node:test';
import assert from 'node:assert/strict';
import { linkIssues, newer, plainItems, render, toRelease, whatsNew } from './releasenotes.ts';

const REPO = 'https://github.com/o/r';
const CI = `FramePort v1.2.0: Windows (x64), macOS (Apple Silicon) and Linux (x64, ARM64).

## What's new

- **Game settings** in plain words
- Fixes: scanning on macOS (#48)

Already have FramePort? It offers this update itself (Library → **Update now**).

## Install

- **Windows:** unzip it`;

test("a release's notes give only its What's new part", () => {
  assert.equal(whatsNew(CI), '- **Game settings** in plain words\n- Fixes: scanning on macOS (#48)');
});

test('older notes without What\'s new keep their own sections, not install help', () => {
  const md = whatsNew('FramePort v0.2.0: Windows (x64).\n\n## First launch\n\nWarnings.\n\n## Rift games\n\nScan a folder.\n\n## Verify a download\n\nSums.');
  assert.equal(md, '#### Rift games\n\nScan a folder.');
  assert.equal(whatsNew('FramePort v0.1.0: Windows (x64).\n\n## First launch\n\nWarnings.'), '');
});

test('dev builds show what to test', () => {
  const notes = 'Dev build **1.2.1.dev7** from commit abc (2026-10-10). For testing.\n\n## Please test\n\nGamepads (#162).\n\n## Changes since v1.2.0\n\n- lots';
  assert.equal(whatsNew(notes, true), 'Gamepads (#162).');
  const r = toRelease({ tag: 'dev', name: 'FramePort dev build 1.2.1.dev7', date: '2026-10-10', url: 'u', dev: true, notes }, REPO);
  assert.equal(r.version, '1.2.1.dev7');
  assert.equal(r.kind, 'dev');
});

test('issue numbers become links, not inside code', () => {
  assert.equal(linkIssues('fixed (#48) and `#9`', REPO), `fixed ([#48](${REPO}/issues/48)) and \`#9\``);
});

test('fix roundups get a tag and items become plain text', () => {
  const html = render(whatsNew(CI), REPO);
  assert.match(html, /<li class="fixes"><span class="tag">Fixes<\/span> scanning on macOS/);
  assert.match(html, /<a rel="noopener" href="https:\/\/github.com\/o\/r\/issues\/48">#48<\/a>/);
  assert.deepEqual(plainItems(whatsNew(CI)), ['Game settings in plain words', 'Fixes: scanning on macOS (#48)']);
});

test('releases are features or fixes; the first one says so', () => {
  assert.equal(toRelease({ tag: 'v1.2.0', name: '', date: '', url: '', dev: false, notes: CI }, REPO).kind, 'feature');
  assert.equal(toRelease({ tag: 'v1.2.1', name: '', date: '', url: '', dev: false, notes: CI }, REPO).kind, 'fix');
  assert.match(toRelease({ tag: 'v0.1.0', name: '', date: '', url: '', dev: false, notes: '## First launch\n\nx' }, REPO).html, /The first release/);
});

test('versions sort like the app: a release is newer than its dev builds', () => {
  assert.ok(newer('0.12.1.dev253', '0.12.0'));
  assert.ok(newer('0.12.1', '0.12.1.dev253'));
  assert.ok(newer('0.10.0', '0.9.1'));
  assert.ok(!newer('0.9.1', '0.9.1'));
});
