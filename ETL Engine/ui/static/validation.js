const $=id=>document.getElementById(id); let validationRuns=[],templates=[],validationWorkflow='migration',fileLookupMappings=[];const pipelineConfigMode=new URLSearchParams(location.search).get('pipeline_config')==='1';let pipelinePrevalidationRequest=null;try{pipelinePrevalidationRequest=pipelineConfigMode?JSON.parse(sessionStorage.getItem('sf_etl_pipeline_prevalidation_request')||'null'):null}catch(_error){pipelinePrevalidationRequest=null}
document.addEventListener('DOMContentLoaded',async()=>{ $('validation-run').addEventListener('change',renderPipelines); $('validation-pipeline').addEventListener('change',renderObjects); $('validation-template').addEventListener('change',renderObjects); $('validation-mode').addEventListener('change',syncMode); $('validation-scan-mode').addEventListener('change',syncControls); $('validation-file-scan-mode').addEventListener('change',syncFileControls); $('validation-file-query-mode').addEventListener('change',syncFileControls); const fileModeKind=$('validation-file-query-mode-kind'); if(fileModeKind) fileModeKind.addEventListener('change',syncFileModePanels); document.querySelectorAll('#validation-optional-checks input').forEach(x=>x.addEventListener('change',syncControls)); $('validation-bypass-confirm').addEventListener('change',syncBypass); $('validation-bypass-stage').addEventListener('click',()=>bypassValidation(false)); $('validation-bypass-pipeline').addEventListener('click',()=>bypassValidation(true)); $('validation-tab-migration').addEventListener('click',()=>setValidationWorkflow('migration')); $('validation-tab-file').addEventListener('click',()=>setValidationWorkflow('file')); $('validation-tab-column-analysis').addEventListener('click',()=>setValidationWorkflow('column-analysis')); $('validation-file-object').addEventListener('change',()=>{loadValidationMatchFields();}); const detectBtn=$('validation-file-detect-lookups'); if(detectBtn) detectBtn.addEventListener('click',detectFileLookupMappings); const refTpl=$('validation-file-reference-template'); if(refTpl) refTpl.addEventListener('change',prefillLookupsFromTemplate); $('validation-run-button').addEventListener('click',startValidation); syncPhase(); setValidationWorkflow('migration'); syncFileControls(); syncFileModePanels(); await Promise.allSettled([loadRuns(),loadTemplates(),loadValidationTargetObjects()]); });
async function loadValidationTargetObjects(){try{const response=await fetch('/api/objects?org=target',{cache:'no-store'}),data=await readJsonResponse(response);if(!response.ok||data.error)throw Error(data.error||'Unable to load target objects');$('validation-file-object').innerHTML='<option value="">Select target object</option>'+(data.objects||[]).map(x=>`<option value="${esc(x.name)}">${esc(x.label||x.name)} (${esc(x.name)})</option>`).join('')}catch(error){$('validation-file-object').innerHTML=`<option value="">${esc(error.message)}</option>`}}
async function loadValidationMatchFields(){const objectName=$('validation-file-object').value,select=$('validation-file-target-key');if(!objectName){select.innerHTML='<option value="">Select a target object first</option>';return}select.innerHTML='<option value="">Loading fields...</option>';try{const response=await fetch(`/api/object/${encodeURIComponent(objectName)}?org=target`,{cache:'no-store'}),data=await readJsonResponse(response);if(!response.ok||data.error)throw Error(data.error||'Unable to load target fields');const fields=(data.fields||[]).filter(x=>x.is_external_id||x.name==='External_Id__c');select.innerHTML=fields.length?fields.map(x=>`<option value="${esc(x.name)}">${esc(x.label||x.name)} (${esc(x.name)})${x.is_external_id?' · External ID':' · Legacy field'}</option>`).join(''):'<option value="">No External ID fields found</option>';if(fields.some(x=>x.name==='External_Id__c'))select.value='External_Id__c'}catch(error){select.innerHTML=`<option value="">${esc(error.message)}</option>`}}
function setValidationWorkflow(mode){validationWorkflow=mode;$('validation-migration-panel').hidden=mode!=='migration';$('validation-file-panel').hidden=mode!=='file';$('validation-column-analysis-panel').hidden=mode!=='column-analysis';$('validation-tab-migration').className=`btn ${mode==='migration'?'btn-primary':'btn-secondary'}`;$('validation-tab-file').className=`btn ${mode==='file'?'btn-primary':'btn-secondary'}`;$('validation-tab-column-analysis').className=`btn ${mode==='column-analysis'?'btn-primary':'btn-secondary'}`;$('validation-run-button').textContent=mode==='file'?'Run File Validation':mode==='column-analysis'?'Analyze Columns':'Run Validation';$('validation-progress-link').hidden=true;if(mode==='file'){syncFileControls();syncFileModePanels();setBanner('ok','File-based: provide CSV/folder path and target match fields')}else if(mode==='column-analysis'){setBanner('ok','Column analysis: enter one or more CSV or folder paths')}showResult('','')}
function syncFileControls(){const full=$('validation-file-scan-mode').value==='full',rest=$('validation-file-query-mode').value==='rest';$('validation-file-sample-size-label').hidden=full;$('validation-file-sample-size').disabled=full;$('validation-file-batch-size-label').hidden=!rest;$('validation-file-batch-size').disabled=!rest}
function syncFileModePanels(){const kindEl=$('validation-file-query-mode-kind');const kind=kindEl?kindEl.value:'object_template';const tmpl=$('validation-file-object-template-panel'),soql=$('validation-file-custom-soql-panel');if(tmpl)tmpl.hidden=kind!=='object_template';if(soql)soql.hidden=kind!=='custom_soql'}
async function loadRuns(){try{const r=await fetch('/api/pipeline/runs?limit=500',{cache:'no-store'}),d=await readJsonResponse(r);if(!r.ok||d.error)throw Error(d.error||'Unable to load runs');validationRuns=(d.runs||[]).filter(x=>x.validation_eligible);$('validation-run').innerHTML=validationRuns.map(x=>`<option value="${esc(x.run_key)}">${esc(x.migration_name||x.run_key)} · ${esc(x.validation_status||x.status)} · ${esc(shortDate(x.created_at))}</option>`).join('');if(validationRuns.some(x=>x.run_key===document.body.dataset.runKey))$('validation-run').value=document.body.dataset.runKey;renderPipelines()}catch(e){setBanner('error',e.message)}}
async function loadTemplates(){try{const [flatResp,sourceResp]=await Promise.all([fetch('/api/flat-files/templates',{cache:'no-store'}),fetch('/api/templates',{cache:'no-store'})]);const flatData=await readJsonResponse(flatResp),sourceData=await readJsonResponse(sourceResp);if(!flatResp.ok||flatData.error)throw Error(flatData.error||'Unable to load Flat Files templates');if(!sourceResp.ok||sourceData.error)throw Error(sourceData.error||'Unable to load Source Org templates');const flat=(flatData.templates||[]).map(x=>({...x,input_mode:'flat_file'}));const source=(sourceData.templates||[]).map(x=>({...x,input_mode:x.input_mode||'salesforce_source'}));templates=[...flat,...source];$('validation-template').innerHTML=templates.map(x=>`<option value="${esc(x.id)}">${esc(x.name||x.id)} · ${x.input_mode==='flat_file'?'Flat Files':'Source Org'} · ${Number(x.file_count||x.object_count||x.objects?.length||x.object_names?.length||0)} object(s)</option>`).join('');$('validation-file-reference-template').innerHTML='<option value="">None — detect or map lookups below</option>'+flat.map(x=>`<option value="${esc(x.id)}">${esc(x.name||x.id)}</option>`).join('');const requested=document.body.dataset.templateId||sessionStorage.getItem('sf_etl_prevalidation_template_id')||'';if(templates.some(x=>x.id===requested)){$('validation-template').value=requested;sessionStorage.setItem('sf_etl_prevalidation_template_id',requested)}renderObjects()}catch(e){setBanner('error',e.message)}}
const selectedRun=()=>validationRuns.find(x=>x.run_key===$('validation-run').value);const selectedPipeline=()=>selectedRun()?.pipelines?.find(x=>x.id===$('validation-pipeline').value);const selectedTemplate=()=>templates.find(x=>x.id===$('validation-template').value);
function renderPipelines(){const p=selectedRun()?.pipelines||[];$('validation-pipeline').innerHTML=p.map(x=>`<option value="${esc(x.id)}">${esc(x.name||x.id)} · ${esc(x.status)}</option>`).join('');if(p.some(x=>x.id===document.body.dataset.pipelineId))$('validation-pipeline').value=document.body.dataset.pipelineId;const k=$('validation-run').value;$('validation-progress-link').hidden=!k;$('validation-progress-link').href=`/pipeline/runs/${encodeURIComponent(k)}`;renderObjects()}
const isPreValidation=()=>document.body.dataset.phase==='pre';
function objectNames(){if(isPreValidation()){if(pipelinePrevalidationRequest){if((pipelinePrevalidationRequest.stages||[]).length===1){const t=selectedTemplate();if(t)return t.objects||t.object_names||[]}return [...new Set((pipelinePrevalidationRequest.stages||[]).flatMap(stage=>stage.object_names||[]))]}const t=selectedTemplate();return t?.objects||t?.object_names||[]}return Object.keys(selectedPipeline()?.objects||{})}
function renderObjects(){const names=objectNames();$('validation-objects').innerHTML=names.length?names.map(n=>`<label class="validation-object-option"><input type="checkbox" value="${esc(n)}" checked> <span>${esc(n)}</span></label>`).join(''):'<span class="pipeline-canvas-subtitle">All configured objects will be validated.</span>'}
function syncPhase(){const pre=isPreValidation();document.querySelectorAll('.pre-option').forEach(x=>x.hidden=!pre);document.querySelectorAll('.post-option').forEach(x=>x.hidden=pre);$('validation-progress-link').hidden=pre||!$('validation-run').value;$('validation-run-button').textContent=pipelinePrevalidationRequest?'Run Pipeline Pre-validation':pre?'Run Pre-validation':'Run Post-validation';if(pipelinePrevalidationRequest){$('validation-template').disabled=(pipelinePrevalidationRequest.stages||[]).length!==1}renderObjects();syncControls();syncBypass();setBanner('ok',pipelinePrevalidationRequest?'Configure checks for the complete validation pipeline':pre?'Ready for full file and metadata pre-validation':'Select a completed or stopped migration run')}
function syncMode(){$('validation-source-kind').disabled=$('validation-mode').value==='stage-target'}
function syncControls(){const pre=isPreValidation(),checked=[...document.querySelectorAll('#validation-optional-checks input:checked')],aware=!pre||checked.some(x=>x.value==='master_detail_references'||x.value==='lookup_references'),full=$('validation-scan-mode').value==='full',idFormat=checked.some(x=>x.value==='salesforce_id_format');document.querySelectorAll('.target-option').forEach(x=>x.hidden=!aware);$('validation-sample-size').disabled=full;$('validation-sample-size-label').hidden=false;$('validation-sample-seed').disabled=full||!aware;$('validation-sample-seed-label').hidden=!aware;$('validation-salesforce-id-length').disabled=!idFormat;$('validation-salesforce-id-length-hint').hidden=idFormat;}
function syncBypass(){const enabled=isPreValidation()&&$('validation-bypass-confirm').checked;$('validation-bypass-stage').disabled=!enabled;$('validation-bypass-pipeline').disabled=!enabled}
async function bypassValidation(openPipeline){const t=selectedTemplate();if(!t)return showResult('error','Select a saved template.');if(t.input_mode!=='flat_file')return showResult('error','Bypass is currently supported only for Flat Files templates.');if(!$('validation-bypass-confirm').checked)return;if(!confirm(`Bypass all pre-validation for "${t.name||t.id}" and stage the original files? This can cause runtime load failures.`))return;const buttons=[$('validation-bypass-stage'),$('validation-bypass-pipeline')];buttons.forEach(b=>b.disabled=true);showResult('running','Bypassing validation and staging original files...');try{const r=await fetch('/api/validation/preflight/bypass',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({template_id:t.id})}),d=await r.json();if(!r.ok||d.error)throw Error(d.error||'Bypass failed');showResult('ok','Validation was bypassed and the template was staged. This action is recorded in Pipeline Runs.');if(openPipeline)window.location.href=d.pipeline_url;else{const link=$('validation-progress-link');link.hidden=false;link.href=d.progress_url;link.textContent='Open Bypass Record'}}catch(e){showResult('error',e.message)}finally{syncBypass()}}
async function startValidation(){
  if(validationWorkflow==='file'&&!isPreValidation())return startFileValidation();
  if(validationWorkflow==='column-analysis'&&!isPreValidation())return startColumnAnalysis();
  const pre=$('validation-phase').value==='pre',objects=[...document.querySelectorAll('#validation-objects input:checked')].map(x=>x.value);let url,payload;
  if(pre){const optional_checks=[...document.querySelectorAll('#validation-optional-checks input:checked')].map(x=>x.value),salesforce_id_length=$('validation-salesforce-id-length').value;if(pipelinePrevalidationRequest){const selectedObjects=new Set(objects),selected=selectedTemplate(),singleStage=(pipelinePrevalidationRequest.stages||[]).length===1;payload={...pipelinePrevalidationRequest,optional_checks,salesforce_id_length,scan_mode:$('validation-scan-mode').value,sample_size:Number($('validation-sample-size').value),sample_seed:Number($('validation-sample-seed').value),query_mode:$('validation-query-mode').value,stages:(pipelinePrevalidationRequest.stages||[]).map(stage=>({...stage,template_id:singleStage&&selected?selected.id:stage.template_id,input_mode:singleStage&&selected?selected.input_mode:stage.input_mode,object_names:singleStage?objects:(stage.object_names||[]).filter(name=>selectedObjects.has(name))}))};url='/api/pipeline/validate';}else{const t=selectedTemplate();if(!t)return showResult('error','Select a saved template.');const relationshipChecks=optional_checks.some(x=>x==='master_detail_references'||x==='lookup_references');url='/api/validation/preflight';payload={template_id:t.id,target_aware:t.input_mode==='flat_file'&&(relationshipChecks||$('validation-target-aware').checked),optional_checks,salesforce_id_length,scan_mode:$('validation-scan-mode').value,sample_size:Number($('validation-sample-size').value),sample_seed:Number($('validation-sample-seed').value),query_mode:$('validation-query-mode').value,objects}}}
  else{const r=selectedRun(),p=selectedPipeline();if(!r||!p)return showResult('error','Select a run and pipeline.');url='/api/validation/run';payload={run_key:r.run_key,pipeline_id:p.id,compare:$('validation-mode').value,source_kind:$('validation-source-kind').value,scan_mode:$('validation-scan-mode').value,sample_size:Number($('validation-sample-size').value),sample_seed:Number($('validation-sample-seed').value),query_mode:$('validation-query-mode').value,objects,print_soql:$('validation-print-soql').checked}}
  $('validation-run-button').disabled=true;showResult('running','Starting validation...');
  try{const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),d=await readJsonResponse(r);if(!r.ok||d.error)throw Error(d.error||'Validation failed to start');if(pipelinePrevalidationRequest)sessionStorage.removeItem('sf_etl_pipeline_prevalidation_request');const link=$('validation-progress-link');link.hidden=false;link.href=d.progress_url;link.textContent='Open Validation Progress';showResult('ok',`Validation started. Follow object results and downloads on the progress page.`);setBanner('running','Validation running');window.location.href=d.progress_url}
  catch(e){showResult('error',e.message);setBanner('error',e.message)}finally{$('validation-run-button').disabled=false}
}

function renderColumnAnalysisSummary(data){
  const panel=$('validation-column-analysis-summary');
  if(!panel)return;
  const files=(data.file_summaries||[]).map(item=>`<tr><td>${esc(item.file)}</td><td>${Number(item.total_rows||0).toLocaleString()}</td><td>${Number(item.total_columns||0).toLocaleString()}</td><td>${Number(item.qualifying_columns||0).toLocaleString()}</td></tr>`).join('');
  const downloads=data.downloads||{};
  panel.innerHTML=`<div class="pipeline-canvas-subtitle">Combined rows: <strong>${Number(data.total_rows||0).toLocaleString()}</strong> &middot; qualifying columns: <strong>${Number(data.consolidated_qualifying_columns||0).toLocaleString()}</strong> &middot; completely empty: <strong>${Number(data.completely_empty_columns||0).toLocaleString()}</strong></div><div class="validation-lookup-table-wrap"><table class="validation-lookup-table"><thead><tr><th>File</th><th>Rows</th><th>Columns</th><th>Meets N</th></tr></thead><tbody>${files}</tbody></table></div><div class="ff-action-row"><a class="btn btn-primary" href="${esc(downloads.file_wise||'#')}">Download file-wise report</a><a class="btn btn-secondary" href="${esc(downloads.consolidated||'#')}">Download consolidated report</a></div>`;
  panel.hidden=false;
}

async function startColumnAnalysis(){
  const paths=$('validation-column-analysis-paths').value.split(/\r?\n/).map(value=>value.trim()).filter(Boolean);
  const minimum=Number($('validation-column-analysis-minimum').value);
  if(!paths.length)return showResult('error','Enter at least one CSV file or folder path.');
  if(!Number.isInteger(minimum)||minimum<1)return showResult('error','Minimum records with values must be a whole number of at least 1.');
  const button=$('validation-run-button');
  button.disabled=true;
  $('validation-column-analysis-summary').hidden=true;
  showResult('running','Starting column population analysis...');
  try{
    const response=await fetch('/api/validation/column-analysis',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paths,min_non_empty:minimum})});
    let data=await readJsonResponse(response);
    if(!response.ok||data.error)throw Error(data.error||'Unable to start column analysis');
    const activityKey=data.activity_key;
    while(!['COMPLETED','FAILED'].includes(data.status)){
      const detail=data.current_file?`${data.message} · completed files: ${(data.file_summaries||[]).length}/${data.total_files}`:data.message;
      showResult('running',detail||'Analyzing CSV columns...');
      await new Promise(resolve=>setTimeout(resolve,1000));
      const statusResponse=await fetch(`/api/validation/column-analysis/${encodeURIComponent(activityKey)}`,{cache:'no-store'});
      data=await readJsonResponse(statusResponse);
      if(!statusResponse.ok||data.error)throw Error(data.error||'Unable to read column analysis status');
    }
    if(data.status==='FAILED')throw Error(data.error||data.message||'Column analysis failed');
    renderColumnAnalysisSummary(data);
    showResult('ok',`Analysis completed. ${Number(data.consolidated_qualifying_columns||0).toLocaleString()} consolidated columns meet the minimum of ${minimum.toLocaleString()} non-empty records.`);
    setBanner('ok','Column analysis completed');
  }catch(error){showResult('error',error.message);setBanner('error',error.message)}finally{button.disabled=false}
}

function collectEnabledLookupMappings(){
  return (fileLookupMappings||[]).filter(row=>row && row.enabled && row.external_id).map(row=>({
    source_field: row.source_field,
    target_field: row.target_field,
    referenced_object: row.referenced_object,
    external_id: row.external_id,
    enabled: true,
  }));
}
function renderFileLookupMappings(){
  const body=$('validation-file-lookup-body');
  const note=$('validation-file-lookup-note');
  if(!body) return;
  if(!(fileLookupMappings||[]).length){
    body.innerHTML='<tr class="validation-lookup-empty"><td colspan="5">Click Detect lookup mappings after choosing a CSV path and target object. Optional reference template can also prefill this table.</td></tr>';
    if(note) note.textContent='';
    return;
  }
  body.innerHTML=fileLookupMappings.map((row,index)=>{
    const options=(row.external_id_fields||[]).map(value=>`<option value="${esc(value)}" ${value===row.external_id?'selected':''}>${esc(row.referenced_object)}.${esc(value)}</option>`).join('');
    const disabled=!(row.external_id_fields||[]).length;
    return `<tr>
      <td><input type="checkbox" class="validation-lookup-enabled" data-index="${index}" ${row.enabled?'checked':''} ${disabled?'disabled':''}></td>
      <td>${esc(row.source_field)}</td>
      <td>${esc(row.target_field)}</td>
      <td>${esc(row.referenced_object||'')}</td>
      <td><select class="fh-input validation-lookup-extid" data-index="${index}" ${disabled?'disabled':''}>
        <option value="">Select parent External ID</option>${options}
      </select></td>
    </tr>`;
  }).join('');
  body.querySelectorAll('.validation-lookup-enabled').forEach(input=>{
    input.addEventListener('change',event=>{
      const idx=Number(event.target.dataset.index);
      if(fileLookupMappings[idx]) fileLookupMappings[idx].enabled=!!event.target.checked;
    });
  });
  body.querySelectorAll('.validation-lookup-extid').forEach(select=>{
    select.addEventListener('change',event=>{
      const idx=Number(event.target.dataset.index);
      if(fileLookupMappings[idx]){
        fileLookupMappings[idx].external_id=event.target.value;
        fileLookupMappings[idx].enabled=!!event.target.value;
        renderFileLookupMappings();
      }
    });
  });
}
function mergeFileLookupMappings(rows,{preserveEdits=true}={}){
  const previous=new Map((fileLookupMappings||[]).map(row=>[`${row.source_field}::${row.target_field}`,row]));
  fileLookupMappings=(rows||[]).map(row=>{
    const key=`${row.source_field}::${row.target_field}`;
    const prior=preserveEdits?previous.get(key):null;
    const fields=[...(row.external_id_fields||[])];
    const externalId=(prior&&prior.external_id&&fields.includes(prior.external_id))
      ? prior.external_id
      : (row.external_id||row.recommended_external_id||'');
    const enabled=prior?!!prior.enabled:!!(row.enabled ?? !!externalId);
    return {
      source_field: row.source_field,
      target_field: row.target_field||row.source_field,
      referenced_object: row.referenced_object||'',
      relationship_name: row.relationship_name||'',
      external_id_fields: fields,
      recommended_external_id: row.recommended_external_id||'',
      external_id: externalId,
      enabled: enabled && !!externalId,
    };
  });
  renderFileLookupMappings();
}
async function detectFileLookupMappings(){
  const filePath=$('validation-file-path').value.trim();
  const objectName=$('validation-file-object').value.trim();
  const includeFields=String(($('validation-file-include-fields')&&$('validation-file-include-fields').value)||'').split(',').map(x=>x.trim()).filter(Boolean);
  if(!filePath||!objectName) return showResult('error','Enter the CSV path and target object before detecting lookup mappings.');
  showResult('running','Detecting lookup mappings from CSV columns and target describe...');
  try{
    const response=await fetch('/api/validation/file-lookup-maps',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({file_path:filePath,object_name:objectName,include_fields:includeFields}),
    });
    const data=await readJsonResponse(response);
    if(!response.ok||data.error) throw Error(data.error||'Unable to detect lookup mappings');
    mergeFileLookupMappings(data.mappings||[],{preserveEdits:true});
    const skipped=(data.skipped_polymorphic||[]).length;
    const note=$('validation-file-lookup-note');
    if(note){
      note.textContent=skipped
        ? `Found ${(data.mappings||[]).length} single-parent lookup(s). Skipped ${skipped} polymorphic/unsupported reference column(s).`
        : `Found ${(data.mappings||[]).length} single-parent lookup mapping(s). Edit the parent External ID as needed.`;
    }
    showResult('ok',`Lookup mappings ready (${(data.mappings||[]).length}).`);
  }catch(error){
    showResult('error',error.message);
  }
}
async function prefillLookupsFromTemplate(){
  const templateId=$('validation-file-reference-template').value;
  const objectName=$('validation-file-object').value.trim();
  const filePath=$('validation-file-path').value.trim();
  if(!templateId){
    return;
  }
  if(!objectName||!filePath){
    showResult('error','Enter CSV path and target object before loading a reference template.');
    return;
  }
  showResult('running','Loading lookup mappings from reference template...');
  try{
    // Detect first so we have external_id_fields options, then overlay template choices.
    const detectResponse=await fetch('/api/validation/file-lookup-maps',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({file_path:filePath,object_name:objectName}),
    });
    const detectData=await readJsonResponse(detectResponse);
    if(!detectResponse.ok||detectData.error) throw Error(detectData.error||'Unable to detect lookup mappings');
    const checkResponse=await fetch('/api/validation/file-template-check',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({file_path:filePath,object_name:objectName,reference_template_id:templateId}),
    });
    const check=await readJsonResponse(checkResponse);
    if(!checkResponse.ok||check.error) throw Error(check.error||'Unable to load reference template');
    // Fetch template details via flat-files templates list entry is id-only; re-detect and keep recommended,
    // then call a lightweight merge using template check usable count messaging.
    mergeFileLookupMappings(detectData.mappings||[],{preserveEdits:false});
    // Overlay from template file via dedicated endpoint content already validated; pull mappings from detect + template auto-map style:
    const tplResp=await fetch(`/api/flat-files/templates/${encodeURIComponent(templateId)}`,{cache:'no-store'});
    if(tplResp.ok){
      const tpl=await readJsonResponse(tplResp);
      const fileCfg=(tpl.files||tpl.template?.files||[]).find(item=>String(item.object||'')===objectName);
      const bySource=new Map();
      for(const mapping of (fileCfg?.mappings||[])){
        if(mapping.ignored) continue;
        const source=String(mapping.source||'').trim();
        const target=String(mapping.target||'').trim();
        const externalId=String(mapping.lookup_external_id||mapping.external_id||'').trim();
        const resolution=String(mapping.lookup_resolution||'external_id').trim();
        if(!source||!target||!externalId||resolution==='target_id') continue;
        bySource.set(`${source}::${target}`,{source_field:source,target_field:target,external_id:externalId,referenced_object:mapping.referenced_object||''});
      }
      fileLookupMappings=(fileLookupMappings||[]).map(row=>{
        const hit=bySource.get(`${row.source_field}::${row.target_field}`);
        if(!hit) return row;
        const externalId=(row.external_id_fields||[]).includes(hit.external_id)?hit.external_id:row.external_id;
        return {...row, external_id:externalId, enabled:!!externalId, referenced_object:hit.referenced_object||row.referenced_object};
      });
      renderFileLookupMappings();
    }
    const note=$('validation-file-lookup-note');
    if(note){
      note.textContent=`Prefilled from template "${templateId}". Usable reference mappings: ${check.usable_reference_mappings||0}. Edit as needed.`;
    }
    showResult('ok','Reference template mappings loaded into the table.');
  }catch(error){
    showResult('error',error.message);
  }
}
async function startFileValidation(){
const ignoreEl=$('validation-file-ignore-fields'),includeEl=$('validation-file-include-fields'),soqlEl=$('validation-file-custom-soql'),modeEl=$('validation-file-query-mode-kind');const filePath=$('validation-file-path').value.trim(),objectName=$('validation-file-object').value.trim(),sourceId=$('validation-file-id-column').value.trim(),targetKey=$('validation-file-target-key').value.trim(),templateId=$('validation-file-reference-template').value,fileQueryMode=(modeEl&&modeEl.value)||'object_template',ignoreFields=String((ignoreEl&&ignoreEl.value)||'').split(',').map(x=>x.trim()).filter(Boolean),includeFields=String((includeEl&&includeEl.value)||'').split(',').map(x=>x.trim()).filter(Boolean),customSoql=String((soqlEl&&soqlEl.value)||'').trim(),scanMode=$('validation-file-scan-mode').value,sampleSize=Number($('validation-file-sample-size').value)||1000,queryMode=$('validation-file-query-mode').value,batchSize=Number($('validation-file-batch-size').value)||200,lookupMappings=collectEnabledLookupMappings();if(!filePath||!objectName||!sourceId||!targetKey)return showResult('error','Enter the CSV file or folder path, target object, CSV ID column, and target match field.');if(fileQueryMode==='custom_soql'&&!customSoql)return showResult('error','Enter Custom SOQL (SELECT ... FROM Object). CSV IDs still drive the WHERE clause.');if(scanMode==='sample'&&!(sampleSize>=1&&sampleSize<=1000000))return showResult('error','Sample size must be between 1 and 1,000,000.');if(queryMode==='rest'&&!(batchSize>=25&&batchSize<=500))return showResult('error','REST ID batch size must be between 25 and 500.');const payload={file_source:'path',file_path:filePath,object_name:objectName,source_id_column:sourceId,target_match_field:targetKey,file_query_mode:fileQueryMode,reference_template_id:fileQueryMode==='object_template'?templateId:'',include_fields:fileQueryMode==='object_template'?includeFields:[],ignore_fields:fileQueryMode==='object_template'?ignoreFields:[],exclude_fields:fileQueryMode==='object_template'?ignoreFields:[],custom_soql:fileQueryMode==='custom_soql'?customSoql:'',lookup_mappings:lookupMappings,scan_mode:scanMode,sample_size:sampleSize,query_mode:queryMode,id_chunk_size:batchSize,print_soql:false};$('validation-run-button').disabled=true;try{if(fileQueryMode==='object_template'&&templateId&&!lookupMappings.length){showResult('running','Checking CSV and reference-template compatibility...');const checkResponse=await fetch('/api/validation/file-template-check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),check=await readJsonResponse(checkResponse);if(!checkResponse.ok||check.error)throw Error(check.error||'Unable to check the reference template');if((check.warnings||[]).length){const warning=`Reference template warning:\n\n${check.warnings.map(x=>`• ${x}`).join('\n')}\n\nContinue with usable mappings and skip unsupported references?`;if(!window.confirm(warning)){showResult('error','Validation cancelled. Select another reference template or choose None.');return}}}const readingMessage=`Reading CSV file(s) and matching ${sourceId} to ${targetKey}...`;showResult('running',readingMessage);showValidationLoader('Starting file validation',readingMessage);const response=await fetch('/api/validation/file-run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),data=await readJsonResponse(response);if(!response.ok||data.error)throw Error(data.error||'File validation failed to start');window.location.href=data.progress_url}catch(error){hideValidationLoader();showResult('error',error.message);setBanner('error',error.message)}finally{$('validation-run-button').disabled=false}}

function showValidationLoader(title,detail){const loader=$('validation-loader');if(!loader)return;$('validation-loader-title').textContent=title||'Working...';$('validation-loader-detail').textContent=detail||'Please wait.';loader.hidden=false}
function hideValidationLoader(){const loader=$('validation-loader');if(loader)loader.hidden=true}
function showResult(k,t){const e=$('validation-result');if(!k||!t){e.hidden=true;e.className='validation-top-message';e.textContent='';return}e.hidden=false;e.className=`validation-top-message ${k}`;if(k==='running'){e.innerHTML=`<span class="spinner" aria-hidden="true"></span> ${esc(t)}`}else{e.textContent=t}}function setBanner(k,t){const e=$('validation-banner');e.className=`auth-status ${k}`;e.innerHTML=`<span class="dot"></span> ${esc(t)}`}function shortDate(v){return v?new Date(v).toLocaleString():''}function esc(v){return String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]))}
async function readJsonResponse(response){const text=await response.text();try{return JSON.parse(text)}catch(_error){const plain=text.replace(/<[^>]*>/g,' ').replace(/\s+/g,' ').trim();throw Error(plain?`API returned HTML/non-JSON (HTTP ${response.status}): ${plain.slice(0,300)}`:`Server returned HTTP ${response.status} instead of JSON.`)}}
