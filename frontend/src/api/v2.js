import { http } from '@/utils/request'
import request from '@/utils/request'

// 任务管理
export const taskApi = {
  getTasks(params) {
    return http.get('/api/v2/tasks', params)
  },
  getTask(taskId) {
    return http.get(`/api/v2/tasks/${taskId}`)
  },
  createTask(data) {
    return http.post('/api/v2/tasks', data)
  },
  cancelTask(taskId) {
    return http.post(`/api/v2/tasks/${taskId}/cancel`)
  },
  cancelTasks(taskIds) {
    // 批量取消:一次请求全取消,避免逐个请求中途被打断
    return http.post('/api/v2/tasks/cancel-batch', { task_ids: taskIds })
  },
  retryTask(taskId) {
    return http.post(`/api/v2/tasks/${taskId}/retry`)
  },
  getQueueStatus() {
    return http.get('/api/v2/queue/status')
  },
}

// 发布历史
export const historyApi = {
  getHistory(params) {
    // params: { type?: 'video'|'image', status?, timeRange?, startDate?, endDate?, page, pageSize }
    // 返回: data.items = [{id, type, title, ..., items: [{id, account_name, platform, status, ...}]}, ...]
    return http.get('/api/v2/history', params)
  },
  getBatch(batchId) {
    return http.get(`/api/v2/history/${batchId}`)
  },
  // 删除单条发布历史
  deleteBatch(batchId) {
    return http.delete(`/api/v2/history/${batchId}`)
  },
  // 批量删除发布历史 — 走 axios 实例,因为 http.delete 包装会把第二参序列化成 query
  batchDelete(batchIds) {
    return request.delete('/api/v2/history/batch', { data: { batch_ids: batchIds } })
  },
}

// 作品数据同步（作品回链 + 统计同步）
export const worksApi = {
  // 立即触发一轮统计同步（后端异步执行）；detailIds 缺省时同步全部
  sync(detailIds) {
    return http.post('/api/v2/works/sync', detailIds?.length ? { detail_ids: detailIds } : {})
  },
  // 同步状态: { running, last_synced_at, interval_minutes, enabled }
  syncStatus() {
    return http.get('/api/v2/works/sync-status')
  },
  // 手动补绑一个批次所有成功明细的作品链接，返回绑定结果
  bindWorks(batchId) {
    return http.post(`/api/v2/history/${batchId}/bind-works`)
  },
}

// 统计数据
export const statsApi = {
  getStats() {
    return http.get('/api/v2/stats')
  },
}

// 系统设置
export const settingsApi = {
  getSettings() {
    return http.get('/api/v2/settings')
  },
  updateSettings(data) {
    return http.put('/api/v2/settings', data)
  },
}

// 批量视频发布（发布页视频队列）
export const batchPublishApi = {
  // videos: 发布页每个视频的完整 draft_data 快照数组
  // intervalMinutes: 本次批量发布视频间隔（分钟），仅本次批量生效；
  //                 缺省/NaN/负数视为 0（= 立即发布下一个）。后端写 task.batch_interval_minutes。
  batchPublishVideos(videos, intervalMinutes = 0) {
    return http.post('/api/v2/videos/batch-publish', {
      videos,
      interval_minutes: Math.max(0, Number(intervalMinutes) || 0),
    })
  },
}
