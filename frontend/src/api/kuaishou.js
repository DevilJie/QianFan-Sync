import { http } from '@/utils/request'

// 快手创作者平台相关 API(后端 blueprint: backend/blueprints/kuaishou_bp.py)
export const ksApi = {
  // 搜索「添加地点」POI(后端浏览器代发:打开发布页→选地区→拦截 poi/search)
  searchPosition(accountId, keyword = '') {
    return http.get(
      `/api/kuaishou/search-position?account_id=${accountId}&keyword=${encodeURIComponent(keyword || '')}`
    )
  },
}
