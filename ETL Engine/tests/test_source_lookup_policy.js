const assert = require('assert');
const { compileExplicitLookups } = require('../ui/static/source_lookup_policy.js');

const account = {
  source_object: 'Account',
  target_object: 'Account',
  target_external_id: 'External_Id__c',
  match_mapping: { source_field: 'Account_Number__c', target_field: 'External_Id__c' },
};

const compiled = compileExplicitLookups({
  source_object: 'Contact',
  lookup_mappings: {
    AccountId: {
      lookup_resolution: 'external_id',
      referenced_object: 'Account',
      external_id: 'External_Id__c',
    },
    Region__c: {
      lookup_resolution: 'object_template',
      referenced_object: 'Region__c',
      external_id: 'Region_Code__c',
      template_id: 'region_identity',
    },
    Pricebook2Id: {
      lookup_resolution: 'external_id',
      referenced_object: 'Pricebook2',
      external_id: 'Legacy_Pricebook__c',
    },
    BusinessHoursId: {
      lookup_resolution: 'preserve',
      referenced_object: 'BusinessHours',
      external_id: '',
    },
    OwnerId: {
      lookup_resolution: 'idmapper',
      referenced_object: 'User',
      external_id: 'Username',
    },
    ParentId: {
      lookup_resolution: 'preserve',
      referenced_object: 'Account',
      external_id: '',
    },
  },
}, [account]);

assert.deepStrictEqual(compiled.AccountId, {
  referenced_object: 'Account',
  lookup_resolution: 'pipeline_object',
  referenced_source_key: 'Account_Number__c',
});
assert.deepStrictEqual(compiled.Region__c, {
  lookup_resolution: 'object_template',
  referenced_object: 'Region__c',
  external_id: 'Region_Code__c',
  template_id: 'region_identity',
});
assert.deepStrictEqual(compiled.Pricebook2Id, {
  lookup_resolution: 'external_id',
  referenced_object: 'Pricebook2',
  external_id: 'Legacy_Pricebook__c',
});
assert.strictEqual(compiled.BusinessHoursId.lookup_resolution, 'preserve');
assert.strictEqual(compiled.OwnerId.lookup_resolution, 'idmapper');
assert.strictEqual(compiled.ParentId.lookup_resolution, 'preserve');

console.log('6/6 source lookup policy cases passed');
