// Server-issued logical IDs are shared by every category selector.
window.LogicalCategories = {
  groups: function(rows, warehouseId) {
    var groups = new Map();
    (rows || []).filter(function(r) {
      return !warehouseId || String(r.default_warehouse_id) === String(warehouseId);
    }).forEach(function(r) {
      var id = String(r.logical_category_id || r.id);
      if (!groups.has(id)) groups.set(id, {id:id,name:r.name,category_ids:[],members:[],child_count:0});
      var group=groups.get(id);
      group.category_ids.push(Number(r.id));group.members.push(r);
      group.child_count += Number(r.child_count || 0);
    });
    return Array.from(groups.values());
  },
  memberIds: function(rows, id) {
    var group=this.groups(rows).find(function(g) {return g.id===String(id);});
    return group ? group.category_ids : [];
  },
  options: function(select, rows, warehouseId, physical) {
    this.groups(rows,warehouseId).forEach(function(group) {
      // Write controls are warehouse scoped; refuse ambiguous same-warehouse IDs.
      var ambiguous=physical && group.members.length!==1;
      var codes=group.members.map(function(r){return r.category_display_id;}).filter(Boolean);
      select.append($('<option>').val(physical ? group.category_ids[0] : group.id)
        .text(group.name + (codes.length>1 ? '（'+codes.join(' / ')+'）' : '') + (ambiguous ? '（同仓分类冲突，请先核对）' : ''))
        .prop('disabled',!!ambiguous));
    });
  }
};
