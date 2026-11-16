'use strict';

// Exercise the deployed formatter without Google APIs, email or customer data.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const context = vm.createContext({});
vm.runInContext(fs.readFileSync('apps_script/trigger.gs', 'utf8'), context);
const render = context.batchReportAttentionLines;

const report = {jobs: [
  {index: 1, type: 'PM', state: 'uploaded', order_review: {
    needs_attention: true, status: 'pm_missing_order', message: 'PM missing company order',
    task_url: 'https://app.asana.com/0/0/101'}},
  {index: 2, type: 'CM', state: 'uploaded', order_review: {
    needs_attention: true, status: 'description_updated', message: 'Description verified',
    appended: 'Probe issue\n12/11 16:30', task_url: 'https://app.asana.com/0/0/102'}},
  {index: 3, type: 'CM', state: 'uploaded', order_review: {
    needs_attention: true, status: 'preview', message: 'Draft only',
    draft: 'Button issue\n16/11 15:00'}},
  {index: 4, type: 'CM', state: 'uploaded', order_review: {
    needs_attention: true, status: 'update_unverified', message: 'Needs readback review'}},
  {index: 5, type: 'PM', state: 'pending', reason: 'Insufficient evidence'},
  {index: 6, type: 'CM', state: 'uploaded', order_review: {
    needs_attention: false, status: 'company_order_exists'}},
]};
const lines = Array.from(render(report));
assert.equal(lines.length, 5);
assert.match(lines[0], /uploaded.*PM missing company order/);
assert.match(lines[0], /https:\/\/app.asana.com\/0\/0\/101/);
assert.match(lines[1], /Description verified/);
assert.match(lines[1], /Probe issue \/ 12\/11 16:30/);
assert.match(lines[2], /Draft only/);
assert.match(lines[2], /Button issue \/ 16\/11 15:00/);
assert.match(lines[3], /Needs readback review/);
assert.match(lines[4], /Insufficient evidence/);
assert.equal(Array.from(render({})).length, 0);
console.log('Order notification formatter checks passed');
