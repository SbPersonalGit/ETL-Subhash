(function initSourceLookupPolicy(globalScope) {
  function text(value) {
    return String(value || '').trim();
  }

  function objectName(item) {
    return text(item?.source_object || item?.target_object);
  }

  function compileExplicitLookups(item, sourceObjects) {
    const result = {};
    for (const [fieldName, rawConfig] of Object.entries(item?.lookup_mappings || {})) {
      if (!rawConfig || typeof rawConfig !== 'object') continue;
      const config = { ...rawConfig };
      const resolution = text(config.lookup_resolution).toLowerCase();
      const referencedObject = text(config.referenced_object);

      if (['preserve', 'target_id', 'idmapper'].includes(resolution)) {
        result[fieldName] = config;
        continue;
      }

      const parent = referencedObject
        ? sourceObjects.find(candidate => objectName(candidate) === referencedObject)
        : null;
      if (!parent) {
        if (referencedObject || text(config.external_id) || resolution) {
          result[fieldName] = config;
        }
        continue;
      }

      const parentTargetKey = text(
        parent.match_mapping?.target_field || parent.target_external_id,
      );
      if (!parentTargetKey) continue;
      result[fieldName] = {
        referenced_object: referencedObject,
        lookup_resolution: 'pipeline_object',
        referenced_source_key: text(parent.match_mapping?.source_field) || 'Id',
      };
    }
    return result;
  }

  const api = { compileExplicitLookups };
  globalScope.SourceLookupPolicy = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window);
