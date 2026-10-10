import { test } from 'node:test';
import assert from 'node:assert/strict';
import { fileTree, parseTree } from './filetree.ts';

const FAQ = `Games/                                  ← scan this folder
├── Resident Evil 4/                    ← one folder per game
│   ├── VR4.apk                         ← the APK
│   └── com.Armature.VR4/               ← the data folder
│       ├── main.203.com.Armature.VR4.obb
│       └── patch.203.com.Armature.VR4.obb
└── Beat Saber/
    └── com.beatgames.beatsaber.apk     ← just the APK`;

test('a tree listing becomes nested entries with notes', () => {
  const [root] = parseTree(FAQ);
  assert.equal(root.name, 'Games/');
  assert.equal(root.note, 'scan this folder');
  assert.deepEqual(root.children.map((c) => c.name), ['Resident Evil 4/', 'Beat Saber/']);
  const re4 = root.children[0];
  assert.deepEqual(re4.children.map((c) => c.name), ['VR4.apk', 'com.Armature.VR4/']);
  assert.equal(re4.children[1].children.length, 2);
  assert.equal(root.children[1].children[0].note, 'just the APK');
});

test('folders and files are marked, names escaped', () => {
  const html = fileTree(FAQ);
  assert.match(html, /<li class="ft-dir" data-kind="dir">/);
  assert.match(html, /data-kind="apk"[^]*VR4\.apk/);
  assert.match(html, /data-kind="obb"/);
  assert.match(fileTree('a<b>/'), /a&lt;b&gt;\//);
});
