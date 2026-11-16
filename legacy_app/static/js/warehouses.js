// Warehouses management

$(document).ready(function() {
    loadWarehouses();
});

function loadWarehouses() {
    $.ajax({
        url: '/api/warehouses',
        method: 'GET',
        success: function(warehouses) {
            loadRoutingPolicy(warehouses);
            var tbody = $('#warehouses-tbody');
            tbody.empty();

            if (warehouses.length === 0) {
                tbody.append('<tr><td colspan="8" class="text-center text-muted">暂无仓库</td></tr>');
            } else {
                warehouses.forEach(function(warehouse) {
                    var statusBadge = warehouse.is_active ?
                        '<span class="badge badge-success">启用</span>' :
                        '<span class="badge badge-secondary">禁用</span>';

                    var row = `<tr>
                        <td>${warehouse.id}</td>
                        <td><strong>${warehouse.name}</strong></td>
                        <td><small>${warehouse.address}</small></td>
                        <td>¥${warehouse.shipping_fee.toFixed(2)}</td>
                        <td>${statusBadge}</td>
                        <td>${formatDate(warehouse.created_at)}</td>
                        <td>
                            <a class="btn btn-sm btn-outline-primary" href="/admin/warehouses/${warehouse.id}/inventory-image">库存图片设置</a>
                            <button class="btn btn-sm btn-primary btn-action" onclick="editWarehouse(${warehouse.id}, '${warehouse.name}', '${warehouse.address}', ${warehouse.shipping_fee})">
                                <i class="fas fa-edit"></i> 编辑
                            </button>
                            <button class="btn btn-sm btn-warning btn-action" onclick="toggleWarehouse(${warehouse.id}, ${warehouse.is_active})">
                                <i class="fas fa-power-off"></i> ${warehouse.is_active ? '禁用' : '启用'}
                            </button>
                            <button class="btn btn-sm btn-danger btn-action" onclick="deleteWarehouse(${warehouse.id}, '${String(warehouse.name).replace(/'/g, "\'")}')">
                                <i class="fas fa-trash"></i> 删除
                            </button>
                        </td>
                    </tr>`;
                    tbody.append(row);
                });
            }

            $('#loading').hide();
            $('#warehouses-content').show();
        },
        error: function(err) {
            console.error('Failed to load warehouses:', err);
            alert('加载仓库失败');
        }
    });
}

function addWarehouse() {
    var name = $('#warehouseName').val().trim();
    var address = $('#warehouseAddress').val().trim();
    var shippingFee = parseFloat($('#shippingFee').val()) || 0;

    if (!name || !address) {
        alert('❌ 请填写完整的仓库信息\n\n仓库名称和地址不能为空');
        return;
    }

    // 验证仓库名称是否已存在（简单前端验证）
    var existingNames = [];
    $('#warehouses-tbody tr').each(function() {
        var warehouseName = $(this).find('td:eq(1)').text().trim();
        existingNames.push(warehouseName);
    });

    if (existingNames.includes(name)) {
        alert(`❌ 仓库 "${name}" 已存在\n\n请使用不同的仓库名称`);
        return;
    }

    $.ajax({
        url: '/api/warehouses/create',
        method: 'POST',
        data: {
            name: name,
            address: address,
            shipping_fee: shippingFee
        },
        success: function(response) {
            alert('✅ 仓库添加成功！\n\n仓库名称: ' + name);
            $('#addWarehouseModal').modal('hide');
            $('#addWarehouseForm')[0].reset();
            loadWarehouses();
        },
        error: function(err) {
            var errorMsg = err.responseJSON?.detail || err.responseJSON?.message || '未知错误';
            alert('❌ 添加失败\n\n错误信息: ' + errorMsg + '\n\n请检查仓库名称是否重复或联系管理员');
        }
    });
}

function editWarehouse(id, name, address, shippingFee) {
    $('#editWarehouseId').val(id);
    $('#editWarehouseName').val(name);
    $('#editWarehouseAddress').val(address);
    $('#editShippingFee').val(shippingFee);
    $('#editWarehouseModal').modal('show');
}

function updateWarehouse() {
    var id = $('#editWarehouseId').val();
    var name = $('#editWarehouseName').val().trim();
    var address = $('#editWarehouseAddress').val().trim();
    var shippingFee = parseFloat($('#editShippingFee').val()) || 0;

    if (!name || !address) {
        alert('请填写完整的仓库信息');
        return;
    }

    $.ajax({
        url: `/api/warehouses/${id}/update`,
        method: 'POST',
        data: {
            name: name,
            address: address,
            shipping_fee: shippingFee
        },
        success: function(response) {
            alert('仓库更新成功');
            $('#editWarehouseModal').modal('hide');
            loadWarehouses();
        },
        error: function(err) {
            alert('更新失败: ' + (err.responseJSON?.message || '未知错误'));
        }
    });
}

function toggleWarehouse(warehouseId, currentStatus) {
    var action = currentStatus ? '禁用' : '启用';
    if (confirm(`确定要${action}此仓库吗？`)) {
        $.ajax({
            url: `/api/warehouses/${warehouseId}/toggle`,
            method: 'POST',
            data: {
                is_active: !currentStatus
            },
            success: function(response) {
                alert(`${action}成功`);
                loadWarehouses();
            },
            error: function(err) {
                alert(`${action}失败: ` + (err.responseJSON?.message || '未知错误'));
            }
        });
    }
}

function formatDate(dateString) {
    return RainbowBusinessTime.format(dateString);
}


function deleteWarehouse(id, name){
  if(!confirm('确定彻底删除仓库「'+name+'」吗？\n删除后列表和仓库数据将移除，无法撤销。\n有库存、商品或历史账务关联时会阻止删除，请先迁移。')) return;
  $.get('/api/products/category-delete-access').done(function(access){
  $.ajax({
    url: '/api/warehouses/' + id + '/delete',
    method: 'POST',
    headers: {'X-Category-CSRF':access.csrf},
    success: function(resp){
      alert(resp.message || '已删除');
      loadWarehouses();
    },
    error: function(xhr){
      alert((xhr.responseJSON && xhr.responseJSON.detail) || '删除失败');
    }
  });
  }).fail(function(){alert('请以管理员登录后重试');});
}

var routingRevision = null;
function loadRoutingPolicy(warehouses){
  $.get('/api/warehouse-routing-policy').done(function(policy){
    routingRevision=policy.revision;
    var box=$('#routing-weights').empty();
    warehouses.forEach(function(w){
      var label=$('<label class="me-3"></label>').text(w.name+' 权重：');
      $('<input type="number" min="1" max="10000" step="1" class="form-control d-inline-block" style="width:100px">')
        .attr('data-routing-warehouse',w.id).val(policy.weights[String(w.id)] || (w.name==='一仓'?6:w.name==='二仓'?4:1)).appendTo(label);
      box.append(label);
    });
    $('#routing-stock-priority').prop('checked',policy.stock_priority);
  }).fail(function(){routingRevision=null;});
}
function saveRoutingPolicy(){
  if(routingRevision===null){alert('分仓设置尚未加载，请刷新');return;}
  var weights={},valid=true;
  $('[data-routing-warehouse]').each(function(){
    var value=Number($(this).val());
    if(!Number.isInteger(value)||value<1||value>10000)valid=false;
    weights[String($(this).attr('data-routing-warehouse'))]=value;
  });
  if(!valid){alert('请填写1至10000的整数权重，例如8和2');return;}
  $.get('/api/products/category-delete-access').done(function(access){
    $.ajax({url:'/api/warehouse-routing-policy',method:'POST',contentType:'application/json',
      headers:{'X-Category-CSRF':access.csrf},data:JSON.stringify({revision:routingRevision,weights:weights,
        stock_priority:$('#routing-stock-priority').prop('checked')}),
      success:function(result){routingRevision=result.revision;alert('已保存，仅影响后续订单');},
      error:function(xhr){alert(xhr.responseJSON?.detail||'保存失败，请刷新后重试');}
    });
  }).fail(function(){alert('请以管理员登录后重试');});
}
