const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(
  path.join(__dirname, '..', 'ui', 'static', 'flat_file_designer.js'),
  'utf8',
);
const start = source.indexOf('function cloneMappingPolicy(');
const end = source.indexOf('\nfunction cloneMappingsForColumns(', start);
assert(start >= 0 && end > start, 'cloneMappingPolicy function was not found');

const context = {};
vm.runInNewContext(`${source.slice(start, end)}\nthis.cloneMappingPolicy = cloneMappingPolicy;`, context);

const shared = {
  source: 'OwnerId',
  target: 'OwnerId',
  lookup_resolution: 'polymorphic_external_id',
  is_polymorphic: true,
  referenced_objects: ['Group', 'User'],
  polymorphic_mappings: [{
    identify_by: 'prefix',
    match_value: '005',
    companion_column: '',
    referenced_object: 'User',
    external_id: 'External_Id__c',
    external_id_fields: ['External_Id__c'],
  }],
};
const cloned = context.cloneMappingPolicy(shared, { source: 'OwnerId' });

assert.strictEqual(cloned.lookup_resolution, 'polymorphic_external_id');
assert.strictEqual(cloned.is_polymorphic, true);
assert.deepStrictEqual(Array.from(cloned.referenced_objects), ['Group', 'User']);
assert.strictEqual(cloned.polymorphic_mappings.length, 1);
assert.strictEqual(cloned.polymorphic_mappings[0].match_value, '005');
assert.strictEqual(cloned.polymorphic_mappings[0].referenced_object, 'User');
assert.strictEqual(cloned.polymorphic_mappings[0].external_id, 'External_Id__c');
assert.notStrictEqual(cloned.polymorphic_mappings, shared.polymorphic_mappings);

console.log('flat-file sibling propagation preserves polymorphic mappings');
