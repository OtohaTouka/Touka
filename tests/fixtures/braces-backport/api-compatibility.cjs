'use strict';

// Ordinary finite API contracts only: no deep, cyclic or amplified inputs.
const assert = require('node:assert/strict');
const path = require('node:path');
const braces = require(path.resolve(process.argv[2]));
const mode = process.argv[3];

if (mode === 'option') {
  const api = process.argv[4];
  assert.ok(['parse', 'compile', 'expand', 'stringify'].includes(api));
  const pattern = '{{a,b},c}';
  const input = api === 'parse' ? pattern : braces.parse(pattern);
  const error = api === 'parse' ? SyntaxError : RangeError;
  assert.throws(
    () => braces[api](input, { maxDepth: 1 }),
    value => value instanceof error && /exceeds max depth \(1\)/.test(value.message),
  );
  const accepted = api === 'parse'
    ? braces.parse(pattern, { maxDepth: 2 }).type
    : braces[api](braces.parse(pattern), { maxDepth: 2 });
  console.log(JSON.stringify({ api, accepted }));
} else if (mode === 'normal') {
  const patterns = ['plain', 'a{b,c}d', '{1..3}', '(a{b,c})', '\\{a,b\\}', '"a,b"'];
  const results = patterns.map(pattern => ({
    pattern,
    compiled: braces.compile(pattern),
    expanded: braces.expand(pattern),
    stringified: braces.stringify(braces.parse(pattern)),
  }));
  assert.deepEqual(braces.expand('a{b,c}d'), ['abd', 'acd']);
  assert.deepEqual(braces.expand('{1..3}'), ['1', '2', '3']);
  assert.equal(braces.stringify(braces.parse('a{b,c}d')), 'a{b,c}d');
  results.push({
    noempty: braces.expand('{,a}', { noempty: true }),
    nodupes: braces.expand('{a,a}', { nodupes: true }),
    escapeInvalid: braces.stringify(braces.parse('{a}'), { escapeInvalid: true }),
  });
  console.log(JSON.stringify(results));
} else {
  throw new Error('Unknown finite compatibility mode');
}
