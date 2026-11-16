// Dashboard functionality

$(document).ready(function() {
    loadStats();
    loadRecentOrders();
    setInterval(loadRecentOrders, 10000); // 每10秒更新一次
});

function loadStats() {
    $.ajax({
        url: '/api/admin/stats',
        method: 'GET',
        success: function(data) {
            $('#total-users').text(data.total_users);
            $('#total-products').text(data.total_products);
            $('#total-orders').text(data.total_orders);
            $('#pending-orders').text(data.pending_orders);
        },
        error: function(err) {
            console.error('Failed to load stats:', err);
        }
    });
}

function loadRecentOrders() {
    $.ajax({
        url: '/api/orders?limit=10',
        method: 'GET',
        success: function(orders) {
            var tbody = $('#orders-tbody');
            tbody.empty();

            if (orders.length === 0) {
                tbody.append('<tr><td colspan="5" class="text-center text-muted">暂无订单</td></tr>');
            } else {
                orders.forEach(function(order) {
                    var statusBadge = getStatusBadge(order.status);
                    var items = order.items.map(item => item.product_name + ' x' + item.quantity).join(', ');

                    var row = `<tr>
                        <td><a href="/admin/orders" class="text-decoration-none">#${order.order_number}</a></td>
                        <td>${order.user_username || order.user_telegram_id}</td>
                        <td>¥${order.total_amount.toFixed(2)}</td>
                        <td>${statusBadge}</td>
                        <td>${formatDate(order.created_at)}</td>
                    </tr>`;
                    tbody.append(row);
                });
            }

            $('#loading').hide();
            $('#recent-orders').show();
        },
        error: function(err) {
            console.error('Failed to load orders:', err);
        }
    });
}

function getStatusBadge(status) {
    var statusMap = {
        'pending': { text: '待处理', class: 'badge-warning' },
        'confirmed': { text: '已确认', class: 'badge-info' },
        'shipped': { text: '已发货', class: 'badge-success' },
        'cancelled': { text: '已取消', class: 'badge-danger' }
    };

    var s = statusMap[status] || { text: status, class: 'badge-secondary' };
    return `<span class="badge ${s.class}">${s.text}</span>`;
}

function formatDate(dateString) {
    return RainbowBusinessTime.format(dateString);
}
