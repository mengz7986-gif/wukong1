// Products management

function catalogPriceAjax(options){
    const deferred=$.Deferred(), settings=Object.assign({},options);
    const success=settings.success,error=settings.error;
    delete settings.success;delete settings.error;
    const send=(request,confirmed)=>$.ajax(request).done((...args)=>deferred.resolve(...args)).fail(xhr=>{
        const detail=xhr.responseJSON&&xhr.responseJSON.detail;
        if(!confirmed&&xhr.status===409&&detail&&detail.code==='PRICE_CONFIRMATION_REQUIRED'){
            if(!window.confirm(detail.messages.join('\n')+'\n'+detail.message)){
                deferred.reject({responseJSON:{detail:'已取消，未修改价格。'}});return;
            }
            send(Object.assign({},request,{data:Object.assign({},request.data,{price_review_token:detail.token})}),true);return;
        }
        if(detail&&detail.code==='PRICE_CONFIRMATION_REQUIRED')xhr.responseJSON.detail='成本或商品已变化，请重新确认；未保存。';
        deferred.reject(xhr);
    });
    const promise=deferred.promise();if(success)promise.done(success);if(error)promise.fail(error);
    send(settings,false);return promise;
}

function catalogAmount(value){
    const raw=String(value).trim();
    return /^\d+(?:\.\d{1,2})?$/.test(raw)&&Number(raw)<=1000000?Number(raw):NaN;
}

let warehouses = [];  // 存储仓库列表
let parentCategories = [];  // 存储父级分类列表
let productCatalog = [];  // 存储商品列表，用于批量添加时检查已有口味
let addProductPricing = null;
let batchProductRows = [];
let batchProductPreviewToken = '';
let batchProductPreviewMaterial = '';
let batchProductErrors = [];
let productFilterLoadToken = 0;
const ADD_PRODUCT_PRICING_KEY = 'tg_wholesale_add_product_pricing';

function normalizeLowStockThreshold(value, defaultValue = 20) {
    var fallback = parseInt(defaultValue, 10);
    if (!Number.isFinite(fallback)) fallback = 20;
    if (value === null || value === undefined || String(value).trim() === '') {
        return Math.max(0, fallback);
    }
    var parsed = parseInt(value, 10);
    return Number.isFinite(parsed) ? Math.max(0, parsed) : Math.max(0, fallback);
}

$(document).ready(function() {
    const saved = new URLSearchParams(location.search);
    ['Warehouse','Category','Search','Status'].forEach(function(key) {
        var value = saved.get('product'+key);
        if (value !== null) $('#productFilter'+key).attr('data-saved-value', value);
    });
    $('#productFilterSearch').val(saved.get('productSearch') || '');
    $('#productFilterStatus').val(saved.get('productStatus') || '');
    $('#productFilterWarehouse,#productFilterCategory,#productFilterStatus,#productFilterSearch').on('change input', function() {
        if (this.id==='productFilterWarehouse') refreshProductCategoryFilter();
        const params = new URLSearchParams(location.search);
        ['Warehouse','Category','Search','Status'].forEach(key => {
            const value = $('#productFilter'+key).val() || '';
            if (value) params.set('product'+key, value); else params.delete('product'+key);
        });
        history.replaceState(null, '', location.pathname + (params.size ? '?' + params : ''));
        loadProducts();
    });
    // 先加载仓库列表和父级分类，再加载商品
    loadWarehouses(function() {
        loadParentCategories(function() {
            loadProducts();
        });
    });

    // 当模态框完全显示后填充仓库列表
    $('#addProductModal').on('shown.bs.modal', function() {
        populateWarehouseSelect('#productWarehouse');
        populateParentSelect('#productParent');
        syncAddProductPricing();
        console.log('添加商品模态框显示，仓库列表:', warehouses.length, '个仓库');
    });

    $('#productParent').on('change', function() {
        syncAddProductPricing();
    });
    $('#productWarehouse,#editProductWarehouse').on('change', function() {
        populateParentSelect(this.id==='productWarehouse' ? '#productParent' : '#editProductParent');
        if(this.id==='productWarehouse')syncAddProductPricing();
    });

    $('#editProductModal').on('shown.bs.modal', function() {
        populateWarehouseSelect('#editProductWarehouse');
        populateParentSelect('#editProductParent');
        console.log('编辑商品模态框显示，仓库列表:', warehouses.length, '个仓库');
    });

    $('#editStockModal').on('shown.bs.modal', function() {
        populateWarehouseSelect('#editStockWarehouse');
    });

    $('#batchProductWarehouse').on('change', previewBatchProducts);
});

function loadParentCategories(callback) {
    $.ajax({
        url: '/api/products/parents?include_inactive=true',
        method: 'GET',
        success: function(data) {
            parentCategories = (data || []).filter(function(parent) {
                var name = String((parent && parent.name) || '');
                return name.indexOf('【历史') !== 0;
            });
            refreshProductCategoryFilter();
            console.log('✅ 父级分类加载成功:', parentCategories.length, '个分类');

            populateParentSelect('#productParent');
            populateParentSelect('#editProductParent');
            renderParentTable();
            syncAddProductPricing();

            if (callback) callback();
        },
        error: function(err) {
            console.error('❌ 加载父级分类失败:', err);
            if (callback) callback();
        }
    });
}

function refreshProductCategoryFilter() {
    const filter=$('#productFilterCategory'), warehouse=$('#productFilterWarehouse').val()||'';
    const current=filter.val()||filter.attr('data-saved-value')||'';
    filter.empty().append($('<option>').val('').text('全部分类'));
    LogicalCategories.options(filter,parentCategories,warehouse);
    filter.val(current);
    if (filter.val()===null) filter.val('');
    filter.removeAttr('data-saved-value');
}

function populateParentSelect(selectId) {
    var $select = $(selectId);
    if ($select.length === 0) {
        return;
    }

    var currentValue = $select.val();
    $select.empty();
    $select.append('<option value="">使用默认分类</option>');

    var warehouseId=$(selectId==='#editProductParent' ? '#editProductWarehouse' : '#productWarehouse').val();
    if(warehouseId)LogicalCategories.options($select,parentCategories,warehouseId,true);

    if (currentValue) {
        $select.val(currentValue);
    }
}

function getPriceProfileFromParent(parentId) {
    if (!parentId) {
        return null;
    }

    var parent = parentCategories.find(function(item) {
        return String(item.id) === String(parentId);
    });
    if (!parent) {
        return null;
    }

    var regularPrice = catalogAmount(parent.regular_price == null ? parent.price : parent.regular_price);
    if (!Number.isFinite(regularPrice)) {
        return null;
    }

    return {
        cost_price: parseFloat(parent.cost_price || 0) || 0,
        reference_price_state: parent.reference_price_state,
        level1_agent_price: parseFloat(parent.level1_agent_price || 0) || 0,
        level2_agent_price: parseFloat(parent.level2_agent_price || 0) || 0,
        regular_price: regularPrice,
        low_stock_threshold: normalizeLowStockThreshold(parent.low_stock_threshold),
        source_name: parent.name || '当前类目'
    };
}

function readSavedAddProductPricing() {
    try {
        var raw = localStorage.getItem(ADD_PRODUCT_PRICING_KEY);
        if (!raw) return null;
        var parsed = JSON.parse(raw);
        if (!parsed || !parsed.regular_price) return null;
        return parsed;
    } catch (err) {
        return null;
    }
}

function saveAddProductPricing(profile) {
    if (!profile) return;
    try {
        localStorage.setItem(ADD_PRODUCT_PRICING_KEY, JSON.stringify(profile));
    } catch (err) {
        // ignore storage failures
    }
}

function formatPriceValue(value) {
    return '¥' + (parseFloat(value || 0) || 0).toFixed(2);
}

function syncAddProductPricing() {
    var parentId = $('#productParent').val();
    var profile = parentId ? getPriceProfileFromParent(parentId) : readSavedAddProductPricing();

    if (!profile) {
        profile = {
            cost_price: 0,
            level1_agent_price: 0,
            level2_agent_price: 0,
            regular_price: 0,
            low_stock_threshold: 20,
            source_name: '未设置'
        };
    }

    addProductPricing = profile;
    $('#costPrice').val(profile.reference_price_state==='SET'||Number(profile.cost_price)>0 ? profile.cost_price : '');
    $('#level1Price').val(profile.level1_agent_price);
    $('#level2Price').val(profile.level2_agent_price);
    $('#regularPrice').val(profile.regular_price);

    var summaryText;
    if (profile.regular_price > 0) {
        summaryText = (profile.source_name || '当前类目') + '：'
            + (profile.reference_price_state==='SET'||Number(profile.cost_price)>0?'成本 '+formatPriceValue(profile.cost_price):(profile.reference_price_state==='UNSET'?'成本未填写':'成本待确认')) + ' / '
            + '合伙人 ' + formatPriceValue(profile.level1_agent_price) + ' / '
            + '超级VIP ' + formatPriceValue(profile.level2_agent_price) + ' / '
            + 'VIP客户 ' + formatPriceValue(profile.regular_price);
    } else {
        summaryText = '当前类目价未设置，请先到“商品分类”里点“同步价格/预警”后再新增口味。';
    }

    $('#addPriceSummary').text(summaryText);
}

function showAddParentModal() {
    $('#parentName').val('');
    const select=$('#parentWarehouse').empty().append($('<option>').val('').text('请选择仓库'));
    warehouses.filter(w=>w.is_active!==false).forEach(w=>select.append($('<option>').val(w.id).text(w.name)));
    select.val($('#productFilterWarehouse').val()||'');
    $('#addParentModal').modal('show');
}

function addParentCategory() {
    var name = $('#parentName').val().trim();

    if (!name) {
        alert('请输入分类名称');
        return;
    }
    if(!$('#parentWarehouse').val()){alert('请选择所属仓库');return;}

    $.ajax({
        url: '/api/products/create',
        method: 'POST',
        data: {
            name: name,
            warehouse_id: $('#parentWarehouse').val(),
            is_parent: true,
            regular_price: 0,
            cost_price: 0,
            level1_agent_price: 0,
            level2_agent_price: 0
        },
        success: function(response) {
            alert('✅ 分类创建成功！');
            $('#addParentModal').modal('hide');
            loadParentCategories(function(){ loadProducts(); });
        },
        error: function(err) {
            alert('❌ 创建失败: ' + (err.responseJSON?.detail || '未知错误'));
        }
    });
}

function migrateToDefaultParent() {
    if (!confirm('确定要将所有没有父级的商品迁移到默认分类吗？\n\n此操作只需执行一次。')) {
        return;
    }

    $.ajax({
        url: '/api/products/migrate_to_default_parent',
        method: 'POST',
        success: function(response) {
            alert('✅ ' + response.message);
            loadProducts();
            loadParentCategories();
        },
        error: function(err) {
            alert('❌ 迁移失败: ' + (err.responseJSON?.detail || '未知错误'));
        }
    });
}

function loadWarehouses(callback) {
    $.ajax({
        url: '/api/warehouses',
        method: 'GET',
        success: function(data) {
            warehouses = data;
            const filter = $('#productFilterWarehouse');
            const current = filter.val() || filter.attr('data-saved-value') || '';
            filter.empty().append($('<option>').val('').text('全部仓库'));
            warehouses.forEach(w => filter.append($('<option>').val(w.id).text(w.name)));
            filter.val(current);
            filter.removeAttr('data-saved-value');
            console.log('✅ 仓库列表加载成功:', warehouses.length, '个仓库');

            // 立即填充现有的选择框
            populateWarehouseSelect('#productWarehouse');
            populateWarehouseSelect('#editProductWarehouse');
            populateWarehouseSelect('#editStockWarehouse');
            populateWarehouseSelect('#batchProductWarehouse');

            if (callback) callback();
        },
        error: function(err) {
            console.error('❌ 加载仓库失败:', err);
            alert('无法加载仓库列表，请刷新页面重试');
        }
    });
}

function populateWarehouseSelect(selectId) {
    var $select = $(selectId);
    if ($select.length === 0) {
        console.warn('⚠️ 选择框不存在:', selectId);
        return;
    }

    // 保存当前选中的值
    var currentValue = $select.val();

    $select.empty();
    $select.append('<option value="">请选择仓库...</option>');

    if (warehouses.length === 0) {
        console.warn('⚠️ 仓库列表为空');
        $select.append('<option value="" disabled>暂无可用仓库</option>');
        return;
    }

    warehouses.forEach(function(warehouse) {
        if (warehouse.is_active) {
            $select.append(`<option value="${warehouse.id}">${warehouse.name}</option>`);
        }
    });

    // 恢复之前选中的值
    if (currentValue) {
        $select.val(currentValue);
    }

    console.log('📦 填充仓库选择框', selectId, '- 选项数量:', $select.find('option').length);
}

function productWarehouseLabel(record, warehouseFilter) {
  if (warehouseFilter) return '';
  const warehouse = warehouses.find(w => String(w.id) === String(record.default_warehouse_id));
  const name = record.default_warehouse_name || (warehouse && warehouse.name) || '未绑定仓库';
  return '<span class="badge bg-light text-dark border ms-2 product-warehouse-label">' + escapeProductHtml(name) + '</span>';
}

function loadProducts() {
    $.ajax({
        url: '/api/products?include_inactive=true',
        method: 'GET',
        success: function(products) {
            var tbody = $('#products-tbody');
            tbody.empty();

            if (products.length === 0) {
                tbody.append('<tr><td colspan="11" class="text-center text-muted">暂无商品</td></tr>');
            } else {
                products.forEach(function(product) {
                    // 跳过父级分类本身
                    if (product.is_parent) {
                        return;
                    }

                    var stockStatus = product.stock > 50 ? 'success' : (product.stock > 10 ? 'warning' : 'danger');
                    var statusBadge = product.is_active ?
                        '<span class="badge badge-success">在售</span>' :
                        '<span class="badge badge-secondary">下架</span>';

                    // 父级分类显示
                    var parentDisplay = product.parent_name || '<span class="text-muted">默认分类</span>';

                    // 仓库显示
                    var warehouseDisplay = product.default_warehouse_name || '<span class="text-muted">未绑定</span>';

                    var row = `<tr>
                        <td>${product.id}</td>
                        <td>${product.name}${product.price_offset ? '<small class="badge badge-info ml-1">动态 +¥'+Number(product.price_offset).toFixed(0)+'</small>' : ''}</td>
                        <td><small class="text-info">${parentDisplay}</small></td>
                        <td class="text-muted">¥${(product.cost_price || 0).toFixed(2)}</td>
                        <td class="text-primary">¥${(product.level1_agent_price || 0).toFixed(2)}</td>
                        <td class="text-info">¥${(product.level2_agent_price || 0).toFixed(2)}</td>
                        <td class="text-success">¥${(product.regular_price || product.price || 0).toFixed(2)}</td>
                        <td>${warehouseDisplay}</td>
                        <td>
                            <span class="badge badge-${stockStatus}">${product.stock}</span>
                        </td>
                        <td>${statusBadge}</td>
                        <td>
                            <button class="btn btn-sm btn-primary btn-action" onclick="editStock(${product.id}, '${product.name}', ${product.stock}, ${product.default_warehouse_id || 0})">
                                <i class="fas fa-edit"></i> 库存
                            </button>
                            <button class="btn btn-sm btn-info btn-action" onclick="editProduct(${product.id}, '${product.name}', ${(product.cost_price || 0)}, ${(product.level1_agent_price || 0)}, ${(product.level2_agent_price || 0)}, ${(product.regular_price || product.price || 0)}, '${(product.description || '').replace(/'/g, "\\'")}', ${product.is_active}, ${product.default_warehouse_id || 0}, ${product.parent_id || 'null'})">
                                <i class="fas fa-cog"></i> 编辑
                            </button>
                            <button class="btn btn-sm ${product.is_active ? 'btn-warning' : 'btn-success'} btn-action" onclick="toggleProduct(${product.id}, ${!product.is_active})">
                                <i class="fas fa-power-off"></i> ${product.is_active ? '下架' : '上架'}
                            </button>
                        </td>
                    </tr>`;
                    tbody.append(row);
                });
            }

            $('#loading').hide();
            $('#products-content').show();
        },
        error: function(err) {
            console.error('Failed to load products:', err);
            alert('加载商品失败');
        }
    });
}

function addProduct() {
    var name = $('#productName').val().trim();
    var parentId = $('#productParent').val();
    var pricing = {
        cost_price: parseFloat($('#costPrice').val()) || 0,
        level1_agent_price: catalogAmount($('#level1Price').val()),
        level2_agent_price: catalogAmount($('#level2Price').val()),
        regular_price: catalogAmount($('#regularPrice').val())
    };
    if ($('#regularPrice').val().trim()==='') {
        pricing = parentId ? (getPriceProfileFromParent(parentId) || pricing) : (readSavedAddProductPricing() || pricing);
    }
    var costRaw = $('#costPrice').val().trim();
    var costPrice = costRaw === '' ? 0 : Number(costRaw);
    var level1Price = catalogAmount(pricing.level1_agent_price);
    var level2Price = catalogAmount(pricing.level2_agent_price);
    var regularPrice = catalogAmount(pricing.regular_price);
    var stock = parseInt($('#initialStock').val()) || 0;
    var lowStockThreshold = normalizeLowStockThreshold($('#lowStockThreshold').val());
    var description = $('#productDescription').val().trim();
    var warehouseId = $('#productWarehouse').val();

    if (!name || ![costPrice,level1Price,level2Price,regularPrice].every(v=>Number.isFinite(v)&&v>=0)) {
        alert('❌ 请先设置类目统一价格\n\n新增商品不再手填客户等级价，请先在“商品分类”里同步该类目的价格。');
        return;
    }

    // 验证价格层级：成本 <= 合伙人价 <= 超级VIP价 <= VIP客户价
    if (!(level1Price <= level2Price && level2Price <= regularPrice)) {
        alert('价格层级：合伙人价 ≤ 超级VIP价 ≤ VIP客户价；售价允许低于成本。');
        return;
    }

    if (!warehouseId) {
        alert('❌ 请选择绑定仓库\n\n每个商品必须绑定到一个仓库');
        return;
    }

    var data = {
        name: name,
        cost_price: costPrice,
        reference_price_state: costRaw === '' ? 'UNSET' : 'SET',
        level1_agent_price: level1Price,
        level2_agent_price: level2Price,
        regular_price: regularPrice,
        initial_stock: stock,
        low_stock_threshold: lowStockThreshold,
        description: description,
        warehouse_id: warehouseId
    };

    if (parentId) {
        data.parent_id = parentId;
    }

    catalogPriceAjax({
        url: '/api/products/create',
        method: 'POST',
        data: data,
        success: function(response) {
            alert('✅ 商品添加成功！\n\n商品名称: ' + name + '\n绑定仓库: ' + response.default_warehouse_name + '\n初始库存: ' + stock + '个');
            saveAddProductPricing(pricing);
            $('#addProductModal').modal('hide');
            $('#addProductForm')[0].reset();
            syncAddProductPricing();
            loadProducts();
            loadParentCategories();
        },
        error: function(err) {
            var errorMsg = err.responseJSON?.detail || err.responseJSON?.message || '未知错误';
            alert('❌ 添加失败\n\n错误信息: ' + errorMsg + '\n\n请检查商品名称是否重复、价格格式是否正确');
        }
    });
}

function showBatchProductModal() {
    populateWarehouseSelect('#batchProductWarehouse');
    if (!$('#batchProductWarehouse').val() && warehouses[0]) {
        $('#batchProductWarehouse').val(String(warehouses[0].id));
    }
    $('#batchProductText').val('');
    populateBatchProductCategories();
    invalidateBatchProductPreview();
    batchProductRows = [];
    batchProductErrors = [];
    renderBatchProductPreview();
    $('#batchProductModal').modal('show');
}

function invalidateBatchProductPreview() {
    batchProductPreviewToken='';batchProductPreviewMaterial='';
    $('.batch-product-execute').prop('disabled',true);
}

function populateBatchProductCategories() {
    const warehouseId=Number($('#batchProductWarehouse').val());
    const select=$('#batchProductCategory').empty().append($('<option>').val('').text('请选择分类'));
    LogicalCategories.options(select,parentCategories,warehouseId,true);
    invalidateBatchProductPreview();
}

function batchProductPayload() {
    return {text:String($('#batchProductText').val()||''),parent_id:Number($('#batchProductCategory').val())||null,
        warehouse_id:Number($('#batchProductWarehouse').val())||null};
}

function batchProductError(xhr) {
    const detail=xhr.responseJSON?.detail;
    if(typeof detail==='string')return detail;
    if(Array.isArray(detail))return detail.map(x=>x.msg||'输入格式错误').join('；');
    return '批量商品操作失败，请检查输入或稍后重试';
}

function findParentByName(name, warehouseId) {
    name = String(name || '').trim();
    if (!name) name = '默认分类';
    var matches=parentCategories.filter(function(parent) {
        return String(parent.name || '').trim() === name && (!warehouseId || Number(parent.default_warehouse_id)===Number(warehouseId));
    });
    return matches.length===1 ? matches[0] : null;
}

function findWarehouseByName(name) {
    name = String(name || '').trim();
    if (!name) return null;
    return warehouses.find(function(warehouse) {
        return String(warehouse.name || '').trim() === name;
    }) || null;
}

function findExistingProductInCategory(parent, productName) {
    productName = String(productName || '').trim();
    if (!parent || !productName) return null;
    return (productCatalog || []).find(function(product) {
        if (!product || product.is_parent) return false;
        return String(product.parent_id || '') === String(parent.id)
            && String(product.name || '').trim() === productName;
    }) || null;
}

function splitBatchProductLine(line) {
    var parts;
    if (line.indexOf('\t') >= 0) {
        parts = line.split('\t').map(function(part) { return String(part || '').trim(); });
    } else {
        parts = line.split(/[,，]/).map(function(part) { return String(part || '').trim(); });
    }
    while (parts.length && parts[parts.length - 1] === '') parts.pop();
    return parts;
}

function looksLikeBatchProductHeader(parts) {
    return /品类|分类|口味|仓库|库存|预警|描述/.test((parts || []).join(''));
}

function isNumberToken(value) {
    var text = String(value == null ? '' : value).trim();
    return text !== '' && Number.isFinite(Number(text));
}

function splitTrailingQuantityToken(value) {
    var text = String(value == null ? '' : value).trim();
    var match = text.match(/^(.+?)(?:\s*[xX*×]\s*)?(-?\d+(?:\.\d+)?)$/);
    if (!match) return null;
    var name = String(match[1] || '').trim().replace(/[，,、:：;；-]+$/g, '').trim();
    var quantity = String(match[2] || '').trim();
    if (!name || !isNumberToken(quantity)) return null;
    return { name: name, quantity: quantity };
}

function parseNameQuantityPairs(parts, startIndex) {
    var pairs = [];
    for (var i = startIndex; i < parts.length;) {
        var name = String(parts[i] || '').trim();
        if (!name) return null;
        if (i + 1 < parts.length && isNumberToken(parts[i + 1])) {
            pairs.push({ name: name, quantity: parts[i + 1] });
            i += 2;
            continue;
        }
        var embedded = splitTrailingQuantityToken(name);
        if (embedded) {
            pairs.push(embedded);
            i += 1;
            continue;
        }
        return null;
    }
    return pairs.length ? pairs : null;
}

function parseNameOnlyList(parts, startIndex) {
    var names = [];
    for (var i = startIndex; i < parts.length; i++) {
        var name = String(parts[i] || '').trim();
        if (!name || isNumberToken(name) || splitTrailingQuantityToken(name)) return null;
        names.push(name);
    }
    return names.length ? names : null;
}

function getDefaultBatchProductWarehouse() {
    var id = $('#batchProductWarehouse').val();
    var name = $('#batchProductWarehouse option:selected').text();
    if (!id || !name) return null;
    return { id: parseInt(id, 10), name: name };
}

function pushBatchProductRow(rows, errors, seen, lineNo, categoryName, productName, warehouseName, stockRaw, thresholdRaw, description) {
    categoryName = categoryName || '默认分类';
    productName = productName || '';
    warehouseName = warehouseName || '';
    var warehouse = findWarehouseByName(warehouseName);
    var parent = findParentByName(categoryName,warehouse && warehouse.id);
    var initialStock = stockRaw === '' || stockRaw == null ? 0 : parseInt(stockRaw, 10);
    var lowStockThreshold = thresholdRaw === '' || thresholdRaw == null
        ? (parent ? normalizeLowStockThreshold(parent.low_stock_threshold) : 20)
        : parseInt(thresholdRaw, 10);
    var rowErrors = [];

    if (!parent) rowErrors.push('品类不存在');
    // 统一价格以后端数据库为准；前端父品类价格可能因缓存/旧接口字段缺失而误判。
    if (!productName) rowErrors.push('口味不能为空');
    if (!warehouseName) rowErrors.push('仓库不能为空');
    if (warehouseName && !warehouse) rowErrors.push('仓库不存在');
    if (!Number.isFinite(initialStock) || initialStock < 0) rowErrors.push('初始库存不能小于0');
    if (!Number.isFinite(lowStockThreshold) || lowStockThreshold < 0) rowErrors.push('少量预警不能小于0');

    var key = warehouseName + '|' + categoryName + '|' + productName;
    if (productName && seen[key]) rowErrors.push('本次粘贴中重复');
    if (parent && productName && findExistingProductInCategory(parent, productName)) rowErrors.push('已存在同品类口味');
    seen[key] = true;

    if (rowErrors.length) {
        rowErrors.forEach(function(msg) { errors.push('第' + lineNo + '行' + msg); });
    }

    rows.push({
        line_no: lineNo,
        category_name: categoryName,
        product_name: productName,
        warehouse_name: warehouseName,
        initial_stock: Number.isFinite(initialStock) ? initialStock : 0,
        low_stock_threshold: Number.isFinite(lowStockThreshold) ? lowStockThreshold : 20,
        description: String(description || '').trim(),
        errors: rowErrors
    });
}

function parseBatchProductText(text) {
    var rows = [];
    var errors = [];
    var seen = {};
    var defaultWarehouse = getDefaultBatchProductWarehouse();
    String(text || '').split(/\r?\n/).forEach(function(line, index) {
        var raw = line.trim();
        if (!raw || raw.indexOf('#') === 0) return;
        var parts = splitBatchProductLine(raw);
        if (rows.length === 0 && looksLikeBatchProductHeader(parts)) return;
        if (parts.length < 3) {
            errors.push('第' + (index + 1) + '行列数不足');
            return;
        }

        var categoryName = parts[0] || '默认分类';

        if (defaultWarehouse) {
            var pairs = parseNameQuantityPairs(parts, 1);
            if (pairs) {
                pairs.forEach(function(pair) {
                    pushBatchProductRow(rows, errors, seen, index + 1, categoryName, pair.name, defaultWarehouse.name, pair.quantity, '', '');
                });
                return;
            }
            var namesOnly = parseNameOnlyList(parts, 1);
            if (namesOnly) {
                namesOnly.forEach(function(name) {
                    pushBatchProductRow(rows, errors, seen, index + 1, categoryName, name, defaultWarehouse.name, '0', '', '');
                });
                return;
            }
            if (isNumberToken(parts[2])) {
                pushBatchProductRow(rows, errors, seen, index + 1, categoryName, parts[1] || '', defaultWarehouse.name, parts[2], parts[3] || '', parts.slice(4).join(','));
                return;
            }
        }

        if (!defaultWarehouse && isNumberToken(parts[2])) {
            errors.push('第' + (index + 1) + '行缺少仓库：请选择默认入库仓库，或在第三列填写仓库名');
            return;
        }

        if (parts.length < 4) {
            errors.push('第' + (index + 1) + '行列数不足');
            return;
        }
        pushBatchProductRow(rows, errors, seen, index + 1, categoryName, parts[1] || '', parts[2] || '', parts[3] || '0', parts[4] || '', parts.slice(5).join(','));
    });
    return {rows: rows, errors: errors};
}

function renderBatchProductPreview() {
    var $body = $('#batchProductPreviewBody').empty();
    if (!batchProductRows.length) {
        $body.append('<tr><td colspan="4" class="text-center text-muted">先预览再提交</td></tr>');
    } else {
        batchProductRows.forEach(function(row) {
            var ok = !row.errors || !row.errors.length;
            $body.append(
                '<tr>' +
                  '<td>' + escapeProductHtml(row.product_name || '') + '</td>' +
                  '<td>' + escapeProductHtml(row.quantity) + '</td>' +
                  '<td>' + escapeProductHtml(row.current_state || '待校验') + '</td>' +
                  '<td><span class="badge ' + (ok ? 'bg-success' : 'bg-danger') + '">' + escapeProductHtml(ok ? row.execution_result : row.errors.join('；')) + '</span></td>' +
                '</tr>'
            );
        });
    }
    var summary = '预览 ' + batchProductRows.length + ' 行';
    if (batchProductErrors.length) summary += '，发现 ' + batchProductErrors.length + ' 个问题';
    $('#batchProductSummary').text(summary + (batchProductErrors.length ? '：' + batchProductErrors.slice(0, 3).join('；') : ''));
}

function previewBatchProducts() {
    invalidateBatchProductPreview();
    const payload=batchProductPayload(),material=JSON.stringify(payload);
    $('#batchProductSummary').text('正在校验…');
    return $.ajax({url:'/api/products/batch_preview',method:'POST',contentType:'application/json',data:material,
        success:function(result){
            if(JSON.stringify(batchProductPayload())!==material)return;
            batchProductRows=result.items||[];batchProductErrors=result.errors||[];renderBatchProductPreview();
            if(result.valid){batchProductPreviewToken=result.token;batchProductPreviewMaterial=material;$('.batch-product-execute').prop('disabled',false);}
        },error:function(xhr){batchProductRows=[];batchProductErrors=[batchProductError(xhr)];renderBatchProductPreview();}
    });
}

function submitBatchProducts() {
    const payload=batchProductPayload();
    if(!batchProductPreviewToken||JSON.stringify(payload)!==batchProductPreviewMaterial){alert('请先预览校验，再确认整批执行');return;}
    const warnings=[...new Set(batchProductRows.flatMap(row=>row.price_warnings||[]))];
    if (!confirm(warnings.join('\n')+'\n确认整批执行 ' + batchProductRows.length + ' 行吗？已存在口味将跳过；归档口味不恢复历史库存。')) return;
    payload.preview_token=batchProductPreviewToken;
    $('.batch-product-execute').prop('disabled',true);
    $.ajax({
        url: '/api/products/batch_create',
        method: 'POST',
        contentType: 'application/json',
        data: JSON.stringify(payload),
        success: function(resp) {
            invalidateBatchProductPreview();
            alert('整批执行完成：新增 '+resp.created_count+'，恢复 '+resp.restored_count+'，已存在跳过 '+resp.skipped_count);
            $('#batchProductModal').modal('hide');
            loadParentCategories(function(){ loadProducts(); });
        },
        error: function(xhr) {
            invalidateBatchProductPreview();
            $('#batchProductSummary').text(batchProductError(xhr)+'；整批未执行，请重新预览');
        }
    });
}

function editStock(productId, productName, currentStock, currentWarehouseId) {
    $('#editStockProductId').val(productId);
    $('#editStockProductName').val(productName);
    $('#editStockQuantity').val(currentStock);
    $('#editStockModal').modal('show');

    // 设置当前仓库
    setTimeout(function() {
        if (currentWarehouseId) {
            $('#editStockWarehouse').val(currentWarehouseId);
        }
    }, 100);
}

function updateStock() {
    var productId = $('#editStockProductId').val();
    var quantity = parseInt($('#editStockQuantity').val());
    var warehouseId = $('#editStockWarehouse').val();

    if (!warehouseId) {
        alert('请选择仓库');
        return;
    }

    $.ajax({
        url: '/api/stock/update',
        method: 'POST',
        data: {
            product_id: productId,
            quantity: quantity,
            warehouse_id: warehouseId
        },
        success: function(response) {
            alert('库存已更新为 ' + quantity);
            $('#editStockModal').modal('hide');
            loadProducts();
        },
        error: function(err) {
            alert('更新失败: ' + (err.responseJSON?.detail || err.responseJSON?.message || '未知错误'));
        }
    });
}

function editProduct(id, name, costPrice, level1Price, level2Price, regularPrice, description, is_active, warehouseId, parentId) {
    $('#editProductId').val(id);
    $('#editProductName').val(name);
    var referenceProduct = productCatalog.find(p=>p.id===id);
    var referenceState = referenceProduct ? referenceProduct.reference_price_state : 'SET';
    $('#editCostPrice').val(referenceState==='SET'?costPrice:'').data('price-dirty',false)
      .attr('placeholder',referenceState==='UNKNOWN_LEGACY'?'历史成本待确认；填0可明确确认0元':'未填写；填0表示0元');
    $('#editLevel1Price').val(level1Price);
    $('#editLevel2Price').val(level2Price);
    $('#editRegularPrice').val(regularPrice);
    $('#editProductDescription').val(description);
    $('#editProductStatus').prop('checked', is_active);
    $('#editLowStockThreshold').val(arguments.length > 10 ? arguments[10] : 20);
    $('#editProductModal').modal('show');

    // 设置当前仓库和父级分类
    setTimeout(function() {
        if (warehouseId) {
            $('#editProductWarehouse').val(warehouseId);
        }
        populateParentSelect('#editProductParent');
        if (parentId) {
            $('#editProductParent').val(parentId);
        }
    }, 100);
}

$(document).on('input','#editCostPrice',function(){$(this).data('price-dirty',true);});

function updateProduct() {
    var productId = $('#editProductId').val();
    var name = $('#editProductName').val().trim();
    var costPrice = parseFloat($('#editCostPrice').val()) || 0;
    var level1Price = catalogAmount($('#editLevel1Price').val());
    var level2Price = catalogAmount($('#editLevel2Price').val());
    var regularPrice = catalogAmount($('#editRegularPrice').val());
    var description = $('#editProductDescription').val().trim();
    var is_active = $('#editProductStatus').is(':checked');
    var warehouseId = $('#editProductWarehouse').val();
    var parentId = $('#editProductParent').val();
    var lowStockThreshold = normalizeLowStockThreshold($('#editLowStockThreshold').val());

    if (!name || ![costPrice,level1Price,level2Price,regularPrice].every(v=>Number.isFinite(v)&&v>=0)) {
        alert('请填写完整的商品信息');
        return;
    }

    // 验证价格层级
    if (!(level1Price <= level2Price && level2Price <= regularPrice)) {
        alert('价格层级：合伙人价 ≤ 超级VIP价 ≤ VIP客户价；售价允许低于成本。');
        return;
    }

    var data = {
        name: name,
        cost_price: costPrice,
        level1_agent_price: level1Price,
        level2_agent_price: level2Price,
        regular_price: regularPrice,
        low_stock_threshold: lowStockThreshold,
        description: description,
        is_active: is_active
    };
    if (!$('#editCostPrice').data('price-dirty')) delete data.cost_price;
    else data.reference_price_state=$('#editCostPrice').val().trim()===''?'UNSET':'SET';

    // 只有选择了仓库才发送
    if (warehouseId) {
        data.warehouse_id = warehouseId;
    }

    // 只有选择了父级才发送
    if (parentId) {
        data.parent_id = parentId;
    }

    catalogPriceAjax({
        url: `/api/products/${productId}/update`,
        method: 'POST',
        data: data,
        success: function(response) {
            alert('商品更新成功');
            $('#editProductModal').modal('hide');
            loadProducts();
            loadParentCategories();
        },
        error: function(err) {
            alert('更新失败: ' + (err.responseJSON?.detail || err.responseJSON?.message || '未知错误'));
        }
    });
}

async function setCategoryAdmission(id, enabled, previous) {
    if (!confirm(enabled ? '恢复该分类接单？库存与商品原有上架状态保持不变。' : '暂停该分类及全部口味接单，并从机器人可售库存隐藏？不清零库存，不修改历史订单。')) return;
    try {
        const access = await fetch('/api/products/category-delete-access', {cache:'no-store'});
        const token = await access.json(); if (!access.ok) throw Error(token.detail || '管理员校验失败');
        const form = new URLSearchParams({is_active:String(enabled),expected_active:String(previous)});
        const response = await fetch('/api/products/parents/'+Number(id)+'/availability', {
            method:'POST', headers:{'X-Category-CSRF':token.csrf}, body:form});
        const result = await response.json(); if (!response.ok) throw Error(result.detail || '状态修改失败');
        loadParentCategories(loadProducts);
    } catch (error) { alert(error.message); }
}

function toggleProduct(productId, newStatus) {
    var action = newStatus ? '上架' : '下架';
    if (confirm(`确定要${action}此商品吗？`)) {
        $.ajax({
            url: `/api/products/${productId}/update`,
            method: 'POST',
            data: {
                is_active: newStatus
            },
            success: function(response) {
                alert(`${action}成功`);
                loadProducts();
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


function renderParentTable() {
    var tbody = $('#parents-tbody');
    if (!tbody.length) return;
    tbody.empty();
    if (!parentCategories.length) {
        tbody.append('<tr><td colspan="4" class="text-center text-muted">暂无分类</td></tr>');
        return;
    }
    parentCategories.forEach(function(parent) {
        var canDelete = !!parent.id;
        var safeName = String(parent.name).replace(/'/g, "\\'");
        var ops = `<button class="btn btn-sm btn-outline-secondary" onclick="renameParentCategory(${parent.id}, '${safeName}')"><i class="fas fa-pen"></i> 改名</button> `;
        ops += `<button class="btn btn-sm btn-outline-primary" onclick="batchUpdateParentPrices(${parent.id}, '${safeName}')"><i class="fas fa-tags"></i> 同步价格/预警</button> `;
        ops += canDelete
            ? `<button class="btn btn-sm btn-danger" onclick="deleteParentCategory(${parent.id}, '${safeName}')"><i class="fas fa-trash"></i> 删除</button>`
            : '<span class="sk-meta">系统分类</span>';
        tbody.append(`
          <tr>
            <td class="sk-meta">${parent.category_display_id || ('C' + String(parent.category_serial || parent.id).padStart(3, '0'))}</td>
            <td class="sk-strong">${parent.name}</td>
            <td>${parent.child_count || 0}</td>
            <td>${ops}</td>
          </tr>`);
    });
}

function renameParentCategory(parentId, currentName) {
    if (!$('#categoryRenameModal').length) {
        $('body').append('<div class="modal fade" id="categoryRenameModal" tabindex="-1" aria-labelledby="categoryRenameTitle" aria-hidden="true"><div class="modal-dialog"><div class="modal-content"><div class="modal-header"><h5 class="modal-title" id="categoryRenameTitle">分类 / 品牌改名</h5><button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="关闭"></button></div><div class="modal-body"><label for="categoryRenameInput" class="form-label">新名称</label><input id="categoryRenameInput" class="form-control" maxlength="100" autocomplete="off"><div class="form-text">只改显示名称；商品编号、库存/FIFO、价格、历史订单和已审核 Alias 关联保持不变。</div><div id="categoryRenameConfirm" class="alert alert-warning mt-3" hidden></div><div id="categoryRenameError" class="text-danger mt-2" role="alert"></div></div><div class="modal-footer"><button type="button" class="btn btn-secondary" data-bs-dismiss="modal">取消</button><button type="button" id="categoryRenameSubmit" class="btn btn-primary">下一步</button></div></div></div></div>');
    }
    var $input=$('#categoryRenameInput').val(currentName || '');
    $('#categoryRenameConfirm').prop('hidden',true).text('');
    $('#categoryRenameError').text('');
    $('#categoryRenameSubmit').text('下一步').prop('disabled',false).data('confirmed',false).off('click').on('click',function() {
        var nextName=$input.val().trim(),$submit=$(this);
        if (!nextName) { $('#categoryRenameError').text('名称不能为空。'); return; }
        if (nextName === currentName) { $('#categoryRenameError').text('新名称与当前名称相同。'); return; }
        if (!$submit.data('confirmed')) {
            $('#categoryRenameConfirm').prop('hidden',false).text('请再次确认：'+currentName+' → '+nextName+'。');
            $('#categoryRenameError').text('');$submit.data('confirmed',true).text('确认改名');$input.prop('disabled',true);return;
        }
        $submit.prop('disabled',true);
        $.post('/api/products/' + parentId + '/inline_update',{field:'name',value:nextName})
          .done(function(){
              $('#categoryRenameModal').modal('hide');
              loadParentCategories(function(){loadProducts();});
          })
          .fail(function(xhr){
              $('#categoryRenameError').text((xhr.responseJSON&&xhr.responseJSON.detail)||'改名失败，未修改任何数据。');
              $submit.prop('disabled',false);
          });
    });
    $('#categoryRenameModal').off('hidden.bs.modal.rename').on('hidden.bs.modal.rename',function(){
        $input.prop('disabled',false);$('#categoryRenameSubmit').data('confirmed',false);
    }).modal('show');
    setTimeout(function(){$input.trigger('focus').select();},150);
}

function showParentBatchUpdateStatus(type, message) {
    $('#parentBatchUpdateStatus')
        .removeClass('alert-success alert-danger alert-warning alert-info')
        .addClass('alert alert-' + type)
        .text(message)
        .show();
}

function getParentCategory(parentId) {
    return parentCategories.find(function(parent) {
        return String(parent.id) === String(parentId);
    }) || null;
}

function batchUpdateParentPrices(parentId, name) {
    var parent = getParentCategory(parentId) || {};
    $('#parentBatchUpdateId').val(parentId);
    $('#parentBatchUpdateName').text(parent.name || name || '-');
    $('#parentBatchCostPrice').val(Number(parent.cost_price || 0).toFixed(2));
    $('#parentBatchLevel1Price').val(Number(parent.level1_agent_price || 0).toFixed(2));
    $('#parentBatchLevel2Price').val(Number(parent.level2_agent_price || 0).toFixed(2));
    $('#parentBatchRegularPrice').val(Number(parent.regular_price || parent.price || 0).toFixed(2));
    $('#parentBatchLowStockThreshold').val(normalizeLowStockThreshold(parent.low_stock_threshold));
    $('#parentBatchUpdateStatus').hide().text('');
    $('#submitParentBatchUpdateBtn').prop('disabled', false).html('<i class="fas fa-save me-1"></i>确认同步');
    $('#parentBatchUpdateModal').modal('show');
}

function submitParentBatchUpdate() {
    var parentId = $('#parentBatchUpdateId').val();
    var name = $('#parentBatchUpdateName').text() || '该品类';
    var data = {
        cost_price: $('#parentBatchCostPrice').val(),
        level1_agent_price: $('#parentBatchLevel1Price').val(),
        level2_agent_price: $('#parentBatchLevel2Price').val(),
        regular_price: $('#parentBatchRegularPrice').val(),
        low_stock_threshold: $('#parentBatchLowStockThreshold').val()
    };
    var costPrice = parseFloat(data.cost_price);
    var level1Price = catalogAmount(data.level1_agent_price);
    var level2Price = catalogAmount(data.level2_agent_price);
    var regularPrice = catalogAmount(data.regular_price);
    var threshold = parseInt(data.low_stock_threshold, 10);

    if (!parentId) {
        showParentBatchUpdateStatus('danger', '缺少品类信息，请刷新后重试。');
        return;
    }
    if (![costPrice, level1Price, level2Price, regularPrice].every(Number.isFinite)) {
        showParentBatchUpdateStatus('danger', '价格必须填写为数字。');
        return;
    }
    if (costPrice < 0 || level1Price < 0 || level2Price < 0 || regularPrice < 0) {
        showParentBatchUpdateStatus('danger', '价格不能小于0；零售价需明确确认。');
        return;
    }
    if (!(level1Price <= level2Price && level2Price <= regularPrice)) {
        showParentBatchUpdateStatus('danger', '价格层级：合伙人价 ≤ 超级VIP价 ≤ VIP客户价；售价允许低于成本。');
        return;
    }
    if (!Number.isFinite(threshold) || threshold < 0) {
        showParentBatchUpdateStatus('danger', '少量预警值不能小于 0。');
        return;
    }
    $('#submitParentBatchUpdateBtn').prop('disabled', true).html('<i class="fas fa-spinner fa-spin me-1"></i>同步中');
    catalogPriceAjax({url:'/api/products/parents/' + parentId + '/batch_update',method:'POST',data:data})
        .done(function(resp) {
            showParentBatchUpdateStatus('success', (resp && resp.message) || '同步成功');
            loadParentCategories(function(){ loadProducts(); });
            setTimeout(function() {
                $('#parentBatchUpdateModal').modal('hide');
            }, 500);
        })
        .fail(function(xhr) {
            showParentBatchUpdateStatus('danger', (xhr.responseJSON && xhr.responseJSON.detail) || '同步失败');
        })
        .always(function() {
            $('#submitParentBatchUpdateBtn').prop('disabled', false).html('<i class="fas fa-save me-1"></i>确认同步');
        });
}

function deleteParentCategory(parentId, name) {
    if (!$('#categoryDeleteModal').length) $('body').append('<div class="modal fade" id="categoryDeleteModal" tabindex="-1"><div class="modal-dialog"><div class="modal-content"><div class="modal-header"><h5>确认删除分类</h5><button class="btn-close" data-bs-dismiss="modal"></button></div><div class="modal-body"><p id="categoryDeleteName"></p><p>仅允许无未归档商品、无库存且无有效叫法引用的分类删除。保留历史记录，不迁移商品、不修改库存。</p><p id="categoryDeleteError" class="text-danger" role="alert"></p></div><div class="modal-footer"><button class="btn btn-secondary" data-bs-dismiss="modal">返回</button><button id="categoryDeleteSubmit" class="btn btn-danger">确认安全删除</button></div></div></div></div>');
    $('#categoryDeleteName').text('分类：'+name);
    $('#categoryDeleteError').text('');
    $('#categoryDeleteSubmit').prop('disabled',false).off('click').on('click',async function(){
      $(this).prop('disabled',true);
      try {
        const access=await $.get('/api/products/category-delete-access');
        await $.ajax({
        url: '/api/products/parents/' + parentId + '/delete',
        method: 'POST',
        headers:{'X-Category-CSRF':access.csrf},data:{move_to_default:false}});
        $('#categoryDeleteModal').modal('hide');loadParentCategories(function(){loadProducts();});
      } catch(err){$('#categoryDeleteError').text((err.responseJSON&&err.responseJSON.detail)||'删除失败');}
      finally{$('#categoryDeleteSubmit').prop('disabled',false);}
    });
    $('#categoryDeleteModal').modal('show');
}


// ===== 行内点击编辑 =====
function inlineSaveProduct(productId, field, value, $el){
  return catalogPriceAjax({url:'/api/products/' + productId + '/inline_update',method:'POST',data:{field: field, value: value}})
    .done(function(){
      if ($el) $el.removeClass('table-warning').addClass('table-success');
      setTimeout(function(){ if($el) $el.removeClass('table-success'); }, 600);
    })
    .fail(function(xhr){
      alert((xhr.responseJSON && xhr.responseJSON.detail) || '保存失败');
      loadProducts();
    });
}

function editProductOrderLimit(id,current){
  const value=prompt('该标准口味每单最多几支？填写正整数；留空表示不限购。同品牌口味的跨仓商品共享限额。福袋组成暂不适用。',current||'');
  if(value===null)return;
  if(value.trim() && !/^[1-9]\d*$/.test(value.trim())){alert('请填写正整数，或留空取消限购。');return;}
  inlineSaveProduct(id,'max_per_order',value.trim(),null).done(function(){loadProducts();});
}

$(document).on('blur', '[contenteditable="true"][data-inline-field]', function(){
  var $el = $(this);
  var pid = $el.data('product-id');
  var field = $el.data('inline-field');
  var value = $el.text().trim();
  if (field==='cost_price' && ['成本待确认','未填写'].includes(value)) return;
  // price fields may include currency
  if (['cost_price','level1_agent_price','level2_agent_price','regular_price','low_stock_threshold'].indexOf(field) >= 0) {
    value = value.replace(/[¥,\s]/g,'');
  }
  var old = String($el.data('raw-value'));
  if (String(value) === old) return;
  inlineSaveProduct(pid, field, value, $el).done(function(){
    $el.data('raw-value', value);
  });
});

$(document).on('change', 'select.inline-parent, select.inline-warehouse', function(){
  var $sel = $(this);
  inlineSaveProduct($sel.data('product-id'), $sel.data('inline-field'), $sel.val(), $sel.closest('td'));
});

$(document).on('keydown', '[contenteditable="true"][data-inline-field], [contenteditable="true"][data-stock-edit]', function(e){
  if (e.key === 'Enter') { e.preventDefault(); $(this).blur(); }
});

function inlineSaveStock(productId, warehouseId, quantity, $el) {
  return $.post('/api/stock/update', {
      product_id: productId,
      warehouse_id: warehouseId,
      quantity: parseInt(quantity, 10) || 0,
      mode: 'set',
      note: '后台商品表点击修改库存'
    })
    .done(function(){
      if ($el) $el.removeClass('table-warning').addClass('table-success');
      setTimeout(function(){ if($el) $el.removeClass('table-success'); }, 600);
      loadProducts();
    })
    .fail(function(xhr){
      alert((xhr.responseJSON && xhr.responseJSON.detail) || '库存保存失败');
      loadProducts();
    });
}

$(document).on('blur', '[contenteditable="true"][data-stock-edit]', function(){
  var $el = $(this);
  var value = $el.text().trim().replace(/[,\s]/g,'');
  var old = String($el.data('raw-value'));
  if (String(value) === old) return;
  inlineSaveStock($el.data('product-id'), $el.data('warehouse-id'), value, $el);
});

function getChangedProductStockInputs(groupKey) {
  return $('input.stock-inline-input[data-stock-group="' + groupKey + '"]').filter(function(){
    var $input = $(this);
    var value = Math.max(0, parseInt($input.val(), 10) || 0);
    var old = Number($input.data('raw-value') || 0);
    return value !== old;
  });
}

function updateProductStockSaveButton(groupKey) {
  var count = getChangedProductStockInputs(groupKey).length;
  var $btn = $('.stock-save-btn[data-stock-save-group="' + groupKey + '"]');
  $btn.toggleClass('btn-success', count > 0).toggleClass('btn-outline-success', count === 0);
  $btn.text(count > 0 ? ('确认库存修改 (' + count + ')') : '确认库存修改');
}

$(document).on('input change', 'input.stock-inline-input', function(){
  var $input = $(this);
  var value = Math.max(0, parseInt($input.val(), 10) || 0);
  var old = Number($input.data('raw-value') || 0);
  $input.toggleClass('border-warning bg-warning-subtle', value !== old);
  updateProductStockSaveButton($input.data('stock-group'));
});

$(document).on('keydown', 'input.stock-inline-input', function(e){
  if (e.key === 'Enter') {
    e.preventDefault();
    submitProductStockGroup($(this).data('stock-group'));
  }
});

function submitProductStockGroup(groupKey) {
  var $changed = getChangedProductStockInputs(groupKey);
  if (!$changed.length) {
    alert('这个品类没有库存改动。');
    return;
  }
  if (!confirm('确认保存 ' + $changed.length + ' 个库存修改吗？')) return;
  var $btn = $('.stock-save-btn[data-stock-save-group="' + groupKey + '"]');
  $btn.prop('disabled', true).text('保存中...');
  var requests = $changed.map(function(){
    var $input = $(this);
    return $.post('/api/stock/update', {
      product_id: $input.data('product-id'),
      warehouse_id: $input.data('warehouse-id'),
      quantity: Math.max(0, parseInt($input.val(), 10) || 0),
      mode: 'set',
      note: '后台商品管理页批量确认库存'
    });
  }).get();
  $.when.apply($, requests).done(function(){
    alert('库存修改已保存，共 ' + $changed.length + ' 个。');
    loadProducts();
  }).fail(function(xhr){
    alert((xhr.responseJSON && xhr.responseJSON.detail) || '库存保存失败');
    loadProducts();
  });
}

function escapeProductHtml(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, function(ch) {
    return {'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[ch];
  });
}

function productJsText(value) {
  return String(value == null ? '' : value)
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'")
    .replace(/\r?\n/g, ' ');
}

function toggleProductCategory(categoryKey) {
  var target = $('#product-category-' + categoryKey);
  if (target.length) target.collapse('toggle');
}

function selectedProductIds() {
  return $('.product-batch-check:checked').map(function(){ return String(this.value); }).get();
}

function updateSelectedProductCount() {
  $('#selectedProductCount').text(selectedProductIds().length);
}

function selectProductCategory(groupKey, checked) {
  $('.product-batch-check[data-product-group="' + groupKey + '"]').prop('checked', !!checked);
  updateSelectedProductCount();
}

function batchClearSelectedStock() {
  var ids = selectedProductIds();
  if (!ids.length) { alert('请先勾选需要删除库存的口味'); return; }
  if (!confirm('确认清空所选 ' + ids.length + ' 个口味的全部库存吗？\n此操作会写入库存流水。')) return;
  $.post('/api/products/batch_clear_stock', {product_ids: ids.join(',')})
    .done(function(resp){ alert(resp.message || '库存已清空'); loadProducts(); })
    .fail(function(xhr){ alert((xhr.responseJSON && xhr.responseJSON.detail) || '批量删除库存失败'); });
}

function batchArchiveSelectedProducts() {
  var ids = selectedProductIds();
  if (!ids.length) { alert('请先勾选需要删除的口味'); return; }
  if (!confirm('确认删除所选 ' + ids.length + ' 个口味吗？\n有库存的口味不会删除；历史订单仍会保留。')) return;
  $.post('/api/products/batch_archive', {product_ids: ids.join(',')})
    .done(function(resp){ alert(resp.message || '口味已删除'); loadParentCategories(function(){ loadProducts(); }); })
    .fail(function(xhr){ alert((xhr.responseJSON && xhr.responseJSON.detail) || '批量删除口味失败'); });
}

$(document).on('change', '.product-batch-check', updateSelectedProductCount);


function loadProducts() {
  const loadToken = ++productFilterLoadToken;
  const warehouseFilter = $('#productFilterWarehouse').val() || '';
  $.ajax({
    url: '/api/products?include_inactive=true' + (warehouseFilter ? '&warehouse_id='+encodeURIComponent(warehouseFilter) : ''),
    method: 'GET',
    success: function(products) {
      if (loadToken !== productFilterLoadToken) return;
      productCatalog = products || [];
      var container = $('#product-category-list');
      container.empty();
      var list = (products||[]).filter(function(p) {
        if (p.is_parent) return false;
        var name = String(p.name || '');
        var parentName = String(p.parent_name || '');
        if (name.indexOf('【历史') === 0 || parentName.indexOf('【历史') === 0) return false;
        if (warehouseFilter && String(p.default_warehouse_id) !== warehouseFilter) return false;
        const category = $('#productFilterCategory').val();
        if (category && !LogicalCategories.memberIds(parentCategories,category).includes(Number(p.parent_id))) return false;
        const keyword = String($('#productFilterSearch').val() || '').trim().toLowerCase();
        if (keyword && (name+' '+parentName).toLowerCase().indexOf(keyword) < 0) return false;
        const status = $('#productFilterStatus').val();
        if (status === 'active' && (!p.is_active || p.auto_unlisted)) return false;
        if (status === 'inactive' && p.is_active) return false;
        if (status === 'paused' && !p.auto_unlisted) return false;
        return true;
      });
      const total = list.length;
      $('#productPageSummary').text('全部显示 · '+total+' 商品');
      if (!list.length && !parentCategories.length) {
        container.html('<div class="text-center text-muted py-4">暂无商品，请先添加分类和商品口味</div>');
        $('#loading').hide();
        container.show();
        return;
      }
      var groups = {};
      parentCategories.forEach(function(parent) {
        if ((!warehouseFilter || String(parent.default_warehouse_id) === warehouseFilter)
            && !$('#productFilterCategory').val() && !$('#productFilterSearch').val()
            && !$('#productFilterStatus').val()) {
          groups[String(parent.id)] = [];
        }
      });
      list.forEach(function(product) {
        var key = String(product.parent_id || '0');
        if (!groups[key]) groups[key] = [];
        groups[key].push(product);
      });

      var categoryOrder = parentCategories.map(function(parent) { return String(parent.id); });
      Object.keys(groups).sort(function(a, b) {
        var ai = categoryOrder.indexOf(a);
        var bi = categoryOrder.indexOf(b);
        return (ai < 0 ? 9999 : ai) - (bi < 0 ? 9999 : bi);
      }).forEach(function(categoryKey, index) {
        var items = groups[categoryKey];
        var parent = parentCategories.find(function(item) { return String(item.id) === categoryKey; }) || {};
        var categoryName = parent.name || '未分类';
        if (!parent.id || categoryName === '未分类' || String(categoryName).indexOf('【历史') === 0) return;
        var categoryDisplayId = parent.category_display_id || ('C' + String(parent.category_serial || categoryKey).padStart(3, '0'));
        var bundleManageLink = '<a class="btn btn-sm btn-outline-primary text-nowrap category-bundle-button" onclick="event.stopPropagation()" href="/admin/bundles/'+Number(parent.id)+'">🎁 福袋管理</a>';
        var stockUnits = ['支','颗','盒','瓶','包','套','件'];
        var stockTotals = {};
        items.forEach(function(item) {
          var unit = stockUnits.includes(item.unit) ? item.unit : '支';
          stockTotals[unit] = (stockTotals[unit] || 0) + Number(item.stock || 0);
        });
        var totalStock = stockUnits.filter(function(unit) { return Object.prototype.hasOwnProperty.call(stockTotals, unit); })
          .map(function(unit) { return stockTotals[unit] + unit; }).join('、') || '0';
        var collapseId = 'product-category-' + categoryKey.replace(/[^0-9a-z_-]/gi, '');
        var rows = items.map(function(product) {
          var stock = Number(product.stock || 0);
          var threshold = normalizeLowStockThreshold(product.low_stock_threshold);
          var stockStatus = stock <= 0 ? 'danger' : (threshold > 0 && stock <= threshold ? 'warning' : 'success');
          var stockTip = product.auto_unlisted ? 'TG机器人停售' : (stockStatus === 'warning' ? '少量预警' : '正常');
          var productDisplayId = product.product_display_id || ('P' + String(product.product_serial || product.id).padStart(3, '0'));
          var warehouseId = product.default_warehouse_id || (warehouses[0] && warehouses[0].id) || '';
          var statusBadge = product.is_active
            ? '<span class="badge bg-success">后台上架</span>'
            : '<span class="badge bg-secondary">手动下架</span>';
          if (product.auto_unlisted) statusBadge += ' <span class="badge bg-warning text-dark">TG停售</span>';
          return `
            <tr>
              <td><input type="checkbox" class="product-batch-check" value="${product.id}" data-product-group="${collapseId}"></td>
              <td class="sk-meta">${escapeProductHtml(productDisplayId)}</td>
              <td>
                <div contenteditable="true" data-inline-field="name" data-product-id="${product.id}" data-raw-value="${escapeProductHtml(product.name || '')}">
                  <strong>${escapeProductHtml(product.name || '')}</strong>${product.price_offset ? '<span class="badge bg-info ms-2">动态 +¥'+Number(product.price_offset).toFixed(0)+'</span>' : ''}
                </div>
                <div class="small text-muted">${escapeProductHtml(categoryName)}${productWarehouseLabel(product, warehouseFilter)}</div>
                <button type="button" class="btn btn-sm btn-outline-secondary mt-1" onclick="editProductOrderLimit(${Number(product.id)},${Number(product.max_per_order||0)})">${product.max_per_order?'每单最多'+Number(product.max_per_order)+'支':'不限购'} · 设置</button>
              </td>
              <td contenteditable="true" data-inline-field="cost_price" data-product-id="${product.id}" data-raw-value="${product.reference_price_state==='SET'?(product.cost_price||0):product.reference_price_state}">${product.reference_price_state==='UNKNOWN_LEGACY'?'成本待确认':(product.reference_price_state==='UNSET'?'未填写':'¥'+Number(product.cost_price||0).toFixed(2))}</td>
              <td contenteditable="true" data-inline-field="level1_agent_price" data-product-id="${product.id}" data-raw-value="${product.level1_agent_price || 0}">¥${Number(product.level1_agent_price || 0).toFixed(2)}</td>
              <td contenteditable="true" data-inline-field="level2_agent_price" data-product-id="${product.id}" data-raw-value="${product.level2_agent_price || 0}">¥${Number(product.level2_agent_price || 0).toFixed(2)}</td>
              <td contenteditable="true" data-inline-field="regular_price" data-product-id="${product.id}" data-raw-value="${product.regular_price || 0}">¥${Number(product.regular_price || 0).toFixed(2)}</td>
              <td>
                <input type="number" min="0" class="form-control form-control-sm stock-inline-input" style="width:90px" value="${stock}" data-product-id="${product.id}" data-warehouse-id="${warehouseId}" data-stock-group="${collapseId}" data-raw-value="${stock}" title="${stockTip}">
                <div class="small text-muted">${stockTip} · 单位：${['支','颗','盒','瓶','包','套','件'].includes(product.unit)?product.unit:'支'}</div>
              </td>
              <td contenteditable="true" data-inline-field="low_stock_threshold" data-product-id="${product.id}" data-raw-value="${threshold}">${threshold}</td>
              <td>${statusBadge}</td>
              <td class="text-nowrap">
                <button class="btn btn-sm btn-outline-primary" onclick="editProduct(${product.id}, '${productJsText(product.name)}', ${Number(product.cost_price || 0)}, ${Number(product.level1_agent_price || 0)}, ${Number(product.level2_agent_price || 0)}, ${Number(product.regular_price || 0)}, '${productJsText(product.description)}', ${product.is_active ? 'true' : 'false'}, ${product.default_warehouse_id || 0}, ${product.parent_id || 'null'}, ${threshold})">编辑</button>
                <button class="btn btn-sm btn-outline-success" onclick="quickAddStock(${product.id}, ${warehouseId || 0})">入库</button>
                <button class="btn btn-sm btn-outline-secondary" onclick="editProductUnit(${product.id})">单位</button>
              </td>
            </tr>`;
        }).join('');
        if (!rows) {
          rows = '<tr><td colspan="11" class="text-center text-muted py-3">该分类暂无口味</td></tr>';
        }
        var safeCategoryName = productJsText(categoryName);
        var categoryActions = '<button type="button" class="btn btn-sm btn-outline-secondary" onclick="event.stopPropagation(); selectProductCategory(\'' + collapseId + '\', true)">全选口味</button> ';
        categoryActions += '<button type="button" class="btn btn-sm btn-outline-success stock-save-btn" data-stock-save-group="' + collapseId + '" onclick="event.stopPropagation(); submitProductStockGroup(this.dataset.stockSaveGroup)">确认库存修改</button> ';
        categoryActions += '<button class="btn btn-sm btn-outline-primary" onclick="event.stopPropagation(); batchUpdateParentPrices(' + (parent.id || 0) + ', \'' + safeCategoryName + '\')">同步价格/预警</button>';
        if (parent.id) {
          categoryActions += ' <button type="button" class="btn btn-sm '+(parent.is_active === false ? 'btn-success' : 'btn-outline-warning')+'" onclick="event.stopPropagation(); setCategoryAdmission('+Number(parent.id)+','+(parent.is_active === false ? 'true' : 'false')+','+(parent.is_active === false ? 'false' : 'true')+')">'+(parent.is_active === false ? '恢复接单' : '暂停接单')+'</button>';
          categoryActions += ' <button class="btn btn-sm btn-outline-secondary" onclick="event.stopPropagation(); renameParentCategory(' + parent.id + ', \'' + safeCategoryName + '\')">改名</button>';
          categoryActions += ' <button class="btn btn-sm btn-outline-primary" onclick="event.stopPropagation(); editProductUnit('+parent.id+')">计量单位</button>';
          categoryActions += ' <button class="btn btn-sm btn-outline-secondary category-move-button" data-category="'+parent.id+'">所属仓库</button>';
          categoryActions += ' '+bundleManageLink;
          categoryActions += ' <button class="btn btn-sm btn-outline-danger" onclick="event.stopPropagation(); deleteParentCategory(' + parent.id + ', \'' + safeCategoryName + '\')">删除</button>';
        }
        container.append(`
          <div class="border rounded overflow-hidden">
            <button type="button" class="btn btn-light w-100 text-start d-flex justify-content-between align-items-center px-3 py-3" onclick="toggleProductCategory('${categoryKey}')">
              <span><span class="me-2">▶</span><strong>${escapeProductHtml(categoryDisplayId)} ${escapeProductHtml(categoryName)}</strong>${productWarehouseLabel(parent, warehouseFilter)}</span>
              <span class="d-flex align-items-center gap-2"><span><span class="badge bg-light text-dark me-2">${items.length} 个口味</span><span class="sk-meta">库存 ${totalStock}</span></span><span onclick="event.stopPropagation()">${categoryActions}</span></span>
            </button>
            <div id="${collapseId}" class="collapse">
              <div class="table-responsive">
                <table class="table table-hover table-sm mb-0 align-middle">
                  <thead><tr><th></th><th>商品编号</th><th>口味</th><th>成本</th><th>合伙人</th><th>超级VIP</th><th>VIP客户</th><th>库存</th><th>预警</th><th>状态</th><th>操作</th></tr></thead>
                  <tbody>${rows}</tbody>
                </table>
              </div>
            </div>
          </div>`);
      });
      $('#loading').hide();
      container.show();
      updateSelectedProductCount();
    },
    error: function(err) {
      console.error(err);
      alert('加载商品失败');
    }
  });
}

function quickAddStock(productId, defaultWarehouseId){
  // jump-ish: open inventory page inbound mentally via prompt
  var qty = prompt('入库数量', '10');
  if (qty === null) return;
  var cost = prompt('本批次成本（可留空用当前成本）', '');
  var warehouseId = defaultWarehouseId || (warehouses[0] && warehouses[0].id);
  if (!warehouseId) { alert('没有仓库'); return; }
  var data = {product_id: productId, warehouse_id: warehouseId, quantity: parseInt(qty,10)||0};
  if (cost !== null && String(cost).trim() !== '') data.unit_cost = cost;
  $.post('/api/stock/inbound', data).done(function(resp){
    alert('入库成功 +' + resp.quantity);
    loadProducts();
  }).fail(function(xhr){
    alert((xhr.responseJSON&&xhr.responseJSON.detail)||'入库失败');
  });
}
