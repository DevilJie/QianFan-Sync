<template>
  <div class="publish-history-detail">
    <!-- 顶部导航条 -->
    <header class="page-header">
      <el-button link :icon="ArrowLeft" @click="goBack">返回</el-button>
      <div class="header-info">
        <h1 class="batch-title">{{ batch?.title || '加载中...' }}</h1>
        <span v-if="batch" class="status-tag" :class="`status-${batch.status}`">
          {{ statusLabel(batch.status) }}
        </span>
        <!-- 发布链间隔等待中：距自动发布的倒计时（读后端 scheduled_at） -->
        <span
          v-if="batch && batchCountdownMs(batch, now) !== null"
          class="countdown-chip"
          :title="`预定 ${formatTime(batch.scheduled_at)} 自动发布`"
        >
          <el-icon><Timer /></el-icon>
          {{ formatCountdown(batchCountdownMs(batch, now)) }} 后发布
        </span>
        <span v-if="batch?.created_at" class="header-time">{{ formatTime(batch.created_at) }}</span>
      </div>
    </header>

    <div class="detail-body">
      <!-- 左侧：账号栏 -->
      <aside v-if="batch" class="detail-sidebar">
        <AccountSidebar
          :mode="'readonly'"
          :account-groups="readonlyAccountGroups"
          :total-count="batchAccounts.length"
          :selected-platform="null"
          :selected-account-id="selectedAccountId"
          :expanded-groups="expandedGroups"
          :publish-account-ids="readonlyPublishAccountIds"
          :has-account-override="() => false"
          @toggle-group="toggleGroup"
          @select-account="selectAccount"
        />
      </aside>

      <!-- 右侧：主区域 -->
      <main class="detail-main" v-loading="loading">
        <!-- 5xx 重试条 -->
        <div v-if="error" class="error-bar">
          <el-icon><WarningFilled /></el-icon>
          <span>{{ error }}</span>
          <el-button size="small" @click="fetchDetail">重试</el-button>
        </div>

        <!-- 空状态 -->
        <div v-else-if="!selectedItem" class="empty-state">
          <el-icon class="empty-icon"><DocumentRemove /></el-icon>
          <p>该批次暂无账号数据</p>
          <p v-if="batchAccounts.length === 0 && batch?.account_count > 0" class="empty-hint">
            该批次的账号已被全部删除，请前往
            <router-link to="/account-management">账号管理</router-link>
            查看
          </p>
        </div>

        <template v-else>
          <div class="detail-grid">
            <!-- 左列：发布内容 Hero 卡 + 评论列表 -->
            <div class="detail-main-col">
            <section class="hero-card" :class="{ 'hero-card--failed': selectedItem.status === 'failed' }">
              <!-- 失败错误横幅：仅失败时显示 -->
              <div v-if="selectedItem.status === 'failed'" class="error-banner">
                <el-icon :size="18"><CircleCloseFilled /></el-icon>
                <div class="error-banner-body">
                  <strong>发布失败</strong>
                  <span>{{ selectedItem.error_message || '未知错误' }}</span>
                </div>
              </div>

              <div class="hero-body">
                <div class="hero-cover">
                  <img v-if="getCoverUrl(selectedItem)" :src="getCoverUrl(selectedItem)" :alt="batch?.title" />
                  <div v-else class="cover-placeholder">
                    <el-icon :size="40"><Picture /></el-icon>
                  </div>
                </div>
                <div class="hero-info">
                  <div class="hero-title-row">
                    <h2 class="hero-title">{{ getCfgField(selectedItem, 'title') || batch?.title || '无标题' }}</h2>
                    <span class="status-tag" :class="`status-${selectedItem.status}`">{{ statusLabel(selectedItem.status) }}</span>
                  </div>
                  <div class="identity-row">
                    <div class="avatar" :style="{ borderColor: currentPlatformConfig?.color || '#666' }">
                      {{ selectedAccount?.name?.charAt(0) || '?' }}
                    </div>
                    <span class="account-name">{{ selectedAccount?.name || '已删除账号' }}</span>
                    <span v-if="currentPlatformConfig" class="platform-badge" :style="{ background: currentPlatformConfig.color + '20', color: currentPlatformConfig.color }">
                      {{ currentPlatformConfig.name }}
                    </span>
                  </div>
                  <p class="hero-desc">{{ getCfgField(selectedItem, 'description') || batch?.description || '无描述' }}</p>
                  <div v-if="getCfgField(selectedItem, 'tags')?.length" class="hero-tags">
                    <el-tag v-for="t in getCfgField(selectedItem, 'tags')" :key="t" size="small" effect="plain">#{{ t }}</el-tag>
                  </div>
                  <div v-if="getCfgField(selectedItem, 'creationDeclaration')" class="hero-meta">
                    <span class="meta-label">作品声明</span>
                    <span>{{ getCfgField(selectedItem, 'creationDeclaration') }}</span>
                  </div>
                  <div v-if="getCfgField(selectedItem, 'scheduleTime')" class="hero-meta">
                    <span class="meta-label">定时发布时间</span>
                    <span>{{ getCfgField(selectedItem, 'scheduleTime') }}</span>
                  </div>
                </div>
              </div>

              <!-- 底部操作栏：元信息在左，动作在右 -->
              <footer class="hero-footer">
                <span class="footer-meta">
                  发布于 {{ formatTime(selectedItem.created_at) }}
                  <span v-if="selectedItem.duration"> · 耗时 {{ formatDuration(selectedItem.duration) }}</span>
                </span>
                <div class="hero-actions">
                  <el-button v-if="selectedItem.status === 'failed' && batch.type === 'video' && selectedAccount"
                    type="primary" size="small" @click="retryFailedVideo">重新发布</el-button>
                  <el-button
                    v-if="isActiveStatus(selectedItem.status)"
                    type="danger"
                    plain
                    size="small"
                    class="cancel-btn"
                    @click="cancelCurrentTask"
                  >取消发布</el-button>
                  <el-button size="small" :loading="syncing" @click="syncWorks">同步数据</el-button>
                  <el-button
                    v-if="selectedItem.status === 'success' && !selectedItem.publish_url && !selectedItem.work_id"
                    size="small"
                    :loading="rebinding"
                    @click="rebindWorks"
                  >重新关联</el-button>
                  <!-- 已关联但平台不提供作品链接(如视频号): 不显示重新关联
                       (重绑也拿不到链接), 用弱提示说明状态 -->
                  <span
                    v-else-if="selectedItem.status === 'success' && !selectedItem.publish_url && selectedItem.work_id"
                    class="bound-no-link"
                  >已关联作品</span>
                  <el-button
                    v-if="selectedItem.status === 'success' && selectedItem.publish_url"
                    size="small"
                    tag="a"
                    :href="selectedItem.publish_url"
                    target="_blank"
                    rel="noopener noreferrer"
                  >查看发布作品 →</el-button>
                </div>
              </footer>
            </section>

            <!-- 评论区（数据来自后端评论同步 work_comments 表） -->
            <section class="comments-card">
              <header class="comments-header">
                <h3 class="section-title">
                  评论
                  <span class="comment-count">{{ commentTotal }}</span>
                </h3>
                <div class="comments-toolbar">
                  <div class="sort-tabs">
                    <button :class="{ active: commentSort === 'hot' }" @click="commentSort = 'hot'">最热</button>
                    <button :class="{ active: commentSort === 'new' }" @click="commentSort = 'new'">最新</button>
                  </div>
                  <el-button size="small" :icon="ChatDotRound" :disabled="commentDisabled" @click="openCommentDialog">一键评论</el-button>
                  <el-button size="small" :icon="Refresh" :loading="commentsSyncing" :disabled="commentDisabled" @click="syncComments">同步评论</el-button>
                </div>
              </header>

              <div v-if="commentsLoading" class="comments-empty">评论加载中…</div>
              <div v-else-if="!displayComments.length" class="comments-empty">
                暂无评论数据，点击右上角「同步评论」从平台拉取
              </div>
              <div v-else class="comment-list">
                <div v-for="(c, i) in displayComments" :key="c.id" class="comment-item">
                  <div class="comment-avatar" :style="{ background: avatarColor(i) }">{{ c.author.charAt(0) }}</div>
                  <div class="comment-main">
                    <div class="comment-head">
                      <span class="comment-author">{{ c.author }}</span>
                      <span v-if="c.isAuthor" class="author-chip">UP主</span>
                      <span v-if="c.pinned" class="pin-chip">置顶</span>
                      <span class="comment-time">{{ c.time }} · {{ c.location }}</span>
                    </div>
                    <p class="comment-content">{{ c.content }}</p>
                    <div class="comment-ops">
                      <span class="op"><el-icon><CaretTop /></el-icon>{{ c.like_count }}</span>
                      <span class="op"><el-icon><ChatDotRound /></el-icon>回复</span>
                    </div>
                    <!-- 楼中楼回复 -->
                    <div v-if="visibleReplies(c).length" class="comment-replies">
                      <div v-for="(r, ri) in visibleReplies(c)" :key="r.id" class="reply-item">
                        <div class="reply-avatar" :style="{ background: avatarColor(ri + 2) }">{{ r.author.charAt(0) }}</div>
                        <div class="reply-body">
                          <div class="reply-head">
                            <span class="reply-author">{{ r.author }}</span>
                            <span v-if="r.isAuthor" class="author-chip">UP主</span>
                          </div>
                          <p class="reply-content">{{ r.content }}</p>
                          <div class="reply-meta">{{ r.time }} · {{ r.location }} · 赞 {{ r.like_count }}</div>
                        </div>
                      </div>
                      <button v-if="repliesToggleLabel(c)" class="replies-toggle" @click="toggleReplies(c)">
                        {{ repliesToggleLabel(c) }}
                      </button>
                    </div>
                  </div>
                </div>
              </div>

              <!-- 一键评论弹窗：输入内容 → 浏览器自动化发评（可选置顶） -->
              <el-dialog v-model="commentDialogVisible" title="发布评论" width="480px" :close-on-click-modal="false" append-to-body>
                <div class="comment-dialog-body">
                  <el-input
                    v-model="commentContent"
                    type="textarea"
                    :rows="4"
                    maxlength="500"
                    show-word-limit
                    placeholder="输入要发布的评论内容（将以该账号身份发布到作品评论区）"
                  />
                  <el-checkbox v-if="commentCaps.supports_pin_comment" v-model="commentPinned" class="comment-pin-check">
                    发布后置顶该评论
                  </el-checkbox>
                  <div v-if="!commentCaps.supports_comment" class="comment-unsupported">
                    当前平台暂未支持评论功能（首期支持 B站）
                  </div>
                </div>
                <template #footer>
                  <el-button @click="commentDialogVisible = false">取消</el-button>
                  <el-button type="primary" :loading="commentPosting" :disabled="!commentContent.trim()" @click="submitComment">
                    {{ commentPosting ? '发布中…' : '发送' }}
                  </el-button>
                </template>
              </el-dialog>
            </section>
            </div>

            <!-- 右列：数据洞察栏（sticky 跟随滚动） -->
            <aside class="insight-rail">
              <section class="data-stats">
                <h3 class="section-title">
                  数据统计
                  <span v-if="selectedItem?.stats?.synced_at" class="synced-at">
                    上次同步 {{ formatSyncedAt(selectedItem.stats.synced_at) }}
                  </span>
                </h3>
                <PublishStats
                  :columns="2"
                  :views="selectedItem?.stats?.views"
                  :likes="selectedItem?.stats?.likes"
                  :favorites="selectedItem?.stats?.favorites"
                  :comments="selectedItem?.stats?.comments"
                />
              </section>
            </aside>
          </div>
        </template>
      </main>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, WarningFilled, DocumentRemove, CircleCloseFilled, Picture, Timer, Refresh, CaretTop, ChatDotRound } from '@element-plus/icons-vue'
import { useAccountStore } from '@/stores/account'
import { accountApi } from '@/api/account'
import { historyApi, taskApi, worksApi, commentsApi } from '@/api/v2'
import { platformList, getPlatformByKey } from '@/config/platforms'
import AccountSidebar from '@/components/AccountSidebar.vue'
import PublishStats from '@/components/PublishStats.vue'
import { useNowTicker, formatCountdown, batchCountdownMs } from '@/composables/useNowTicker'
import { getFileUrl } from '@/utils/storage'

const route = useRoute()
const router = useRouter()
const accountStore = useAccountStore()

const batch = ref(null)
const loading = ref(false)
const error = ref('')
const selectedAccountId = ref(null)
const expandedGroups = reactive(new Set())
const readonlyPublishAccountIds = new Set()  // 空 Set，AccountSidebar 内部不过滤

// 每秒跳动的当前时间：驱动「xx 后发布」倒计时文本
const now = useNowTicker()

const batchAccounts = computed(() => {
  if (!batch.value) return []
  return accountStore.accounts.filter(a =>
    batch.value.items.some(it => it.account_id === a.id)
  )
})

const readonlyAccountGroups = computed(() => {
  return platformList
    .map(p => ({
      key: p.key,
      name: p.name,
      logo: p.logo,
      color: p.color,
      letter: p.letter,
      accounts: batchAccounts.value.filter(a => a.platform === p.name),
    }))
    .filter(g => g.accounts.length > 0)
})

const selectedItem = computed(() => {
  if (!batch.value || !selectedAccountId.value) return null
  return batch.value.items.find(it => it.account_id === selectedAccountId.value) || null
})

const selectedAccount = computed(() => {
  if (!selectedItem.value) return null
  return accountStore.accounts.find(a => a.id === selectedItem.value.account_id) || null
})

const currentPlatformConfig = computed(() => {
  if (!selectedAccount.value) return null
  const key = platformList.find(p => p.name === selectedAccount.value.platform)?.key
  return key ? getPlatformByKey(key) : null
})

function getCfgField(item, field) {
  return item?.account_configs?.[field]
}

function getCoverUrl(item) {
  if (!item) return ''
  const cfg = item.account_configs || {}
  // 优先用 per-account cover 自带的 .url 字段（后端已构造为绝对 URL）
  if (cfg.coverLandscape?.url) return cfg.coverLandscape.url
  if (cfg.coverPortrait?.url) return cfg.coverPortrait.url
  // 文章发布(type=article)：封面存的是 stored_path（如 covers/2026/10/23/uuid.jpg）
  if (cfg.coverStoredPath) return getFileUrl(cfg.coverStoredPath)
  // 兜底：batch 级 cover_url
  return batch.value?.cover_url || ''
}

function statusLabel(status) {
  return ({
    pending: '等待中',
    running: '发布中',
    success: '全部成功',
    partial: '部分失败',
    failed: '全部失败',
    cancelled: '已取消',
  }[status] || status)
}

function formatTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function formatDuration(s) {
  if (s == null) return ''
  if (s < 60) return `${s}秒`
  return `${Math.floor(s / 60)}分${s % 60}秒`
}

function retryFailedVideo() {
  router.push({ path: '/publish-center', query: { retryBatch: batch.value.id, retryItem: selectedItem.value.id } })
}

function goBack() {
  router.push('/publish-history')
}

function isActiveStatus(s) {
  return s === 'pending' || s === 'queued' || s === 'running'
}

async function cancelCurrentTask() {
  if (!selectedItem.value || !isActiveStatus(selectedItem.value.status)) return
  const it = selectedItem.value
  try {
    await ElMessageBox.confirm(
      `确定取消「${selectedAccount.value?.name || it.account_name}」的发布任务？`,
      '取消发布',
      { confirmButtonText: '取消发布', cancelButtonText: '再想想', type: 'warning' },
    )
  } catch { return }
  try {
    await taskApi.cancelTask(it.id)
    ElMessage.success('已请求取消，任务将终止')
    await fetchDetail()
  } catch (e) {
    ElMessage.error('取消失败: ' + (e?.message || e))
  }
}

// ===== 数据统计同步 =====
const syncing = ref(false)
let syncTimer = null

// 本批次所有发布成功明细的 id：本次同步的范围
async function syncWorks() {
  // 只同步当前选中账号的明细(此前是整批次全部成功账号一起同步,
  // 多账号时任意账号点同步所有按钮都转圈、且耗时成倍增长)
  if (syncing.value) return  // 同一时刻只允许一个同步在跑
  const detailId = selectedItem.value?.id
  if (!detailId || selectedItem.value?.status !== 'success') {
    ElMessage.info('当前账号没有发布成功，无需同步')
    return
  }
  syncing.value = true
  try {
    await worksApi.sync([detailId])
  } catch (e) {
    syncing.value = false
    ElMessage.error('触发同步失败: ' + (e?.message || e))
    return
  }
  // 同步是异步任务：每 5s 轮询刷新批次详情，最多 6 次；
  // 期间用 syncStatus 的 running 判断同步是否已结束，结束后再拉一次取最终数据
  let polls = 0
  const finish = (msg, type = 'success') => {
    clearInterval(syncTimer)
    syncTimer = null
    syncing.value = false
    if (msg) ElMessage[type](msg)
  }
  syncTimer = setInterval(async () => {
    polls += 1
    let running = null
    try {
      const res = await worksApi.syncStatus()
      running = !!res?.data?.running
    } catch { /* 状态接口失败不中断轮询，按轮次兜底 */ }
    if (running === false) {
      // 同步已结束 → 再拉一次详情拿最终统计
      await fetchDetailSilent()
      finish('数据同步完成')
      return
    }
    await fetchDetailSilent()
    if (polls >= 6) {
      finish('同步仍在后台进行，稍后可刷新查看最新数据', 'info')
    }
  }, 5000)
}

// ===== 手动补绑作品链接（发布成功但未回填 publish_url 时可用） =====
const rebinding = ref(false)
async function rebindWorks() {
  if (rebinding.value) return
  rebinding.value = true
  try {
    await worksApi.bindWorks(batch.value.id)
    ElMessage.success('重新关联完成')
    // publish_url 回填后「查看发布作品 →」链接会自动出现
    await fetchDetailSilent()
  } catch (e) {
    ElMessage.error('重新关联失败: ' + (e?.message || e))
  } finally {
    rebinding.value = false
  }
}

function formatSyncedAt(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  const pad = n => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

// ===== 评论区（真实数据：后端评论同步 work_comments 表 + 浏览器自动化发评）=====
const commentSort = ref('hot')          // 'hot' 最热（按点赞）| 'new' 最新（按时间）
const expandedReplies = reactive(new Set())  // 已展开楼中楼的评论 id

const comments = ref([])                // 后端 work_comments 的前端投影
const commentsCaps = ref({ supports_comment: false, supports_pin_comment: false })
const commentsLoading = ref(false)
const commentsSyncing = ref(false)
let commentsTimer = null

// 当前明细是否可用评论功能：平台支持 + 明细发布成功
const commentDisabled = computed(() => {
  return !selectedItem.value || selectedItem.value.status !== 'success'
})

async function fetchComments() {
  const detailId = selectedItem.value?.id
  if (!detailId) return
  commentsLoading.value = true
  try {
    const res = await commentsApi.list(detailId)
    commentsCaps.value = {
      supports_comment: !!res?.data?.supports_comment,
      supports_pin_comment: !!res?.data?.supports_pin_comment,
    }
    comments.value = res?.data?.items || []
  } catch (e) {
    comments.value = []
  } finally {
    commentsLoading.value = false
  }
}

// 切换选中账号 / 重新拉取批次详情时，重新加载该明细的评论
watch(() => selectedItem.value?.id, (id) => {
  comments.value = []
  if (commentsTimer) { clearInterval(commentsTimer); commentsTimer = null }
  if (id) fetchComments()
})

// 轮询评论任务（发评/同步都要开浏览器，30~90s）：每 3s 一次，最多 60 次（3 分钟）
function pollCommentTask(taskId, onDone) {
  let polls = 0
  clearInterval(commentsTimer)
  commentsTimer = setInterval(async () => {
    polls += 1
    let st = null
    try {
      const res = await commentsApi.taskStatus(taskId)
      st = res?.data
    } catch { /* 网络抖动不中断轮询 */ }
    if (st && st.status === 'done') {
      clearInterval(commentsTimer)
      commentsTimer = null
      await fetchComments()
      onDone(null, st.result)
    } else if (st && st.status === 'error') {
      clearInterval(commentsTimer)
      commentsTimer = null
      onDone(st.error || '任务失败', null)
    } else if (polls >= 60) {
      clearInterval(commentsTimer)
      commentsTimer = null
      onDone('任务超时，请稍后刷新查看', null)
    }
  }, 3000)
}

// ===== 同步评论：浏览器自动化抓取评论区 → 落库 → 重新渲染 =====
async function syncComments() {
  const detailId = selectedItem.value?.id
  if (!detailId || commentsSyncing.value) return
  commentsSyncing.value = true
  try {
    const res = await commentsApi.sync(detailId)
    const taskId = res?.data?.task_id
    if (!taskId) throw new Error('未返回任务 ID')
    pollCommentTask(taskId, (err) => {
      commentsSyncing.value = false
      if (err) ElMessage.error('同步失败: ' + err)
      else ElMessage.success('评论同步完成')
    })
  } catch (e) {
    commentsSyncing.value = false
    ElMessage.error('触发同步失败: ' + (e?.message || e))
  }
}

// ===== 一键评论：弹窗输入 → 浏览器自动化发评（可选置顶）=====
const commentDialogVisible = ref(false)
const commentContent = ref('')
const commentPinned = ref(false)
const commentPosting = ref(false)
// 弹窗能力跟随当前明细平台（置顶选项仅支持平台显示）
const commentCaps = computed(() => commentsCaps.value)

function openCommentDialog() {
  if (!selectedItem.value) return
  if (!commentsCaps.value.supports_comment) {
    ElMessage.info('当前平台暂未支持评论功能（首期支持 B站）')
  }
  commentContent.value = ''
  commentPinned.value = false
  commentDialogVisible.value = true
}

async function submitComment() {
  const detailId = selectedItem.value?.id
  const content = commentContent.value.trim()
  if (!detailId || !content || commentPosting.value) return
  commentPosting.value = true
  try {
    const res = await commentsApi.post(detailId, content, commentPinned.value)
    const taskId = res?.data?.task_id
    if (!taskId) throw new Error('未返回任务 ID')
    pollCommentTask(taskId, (err, result) => {
      commentPosting.value = false
      if (err) {
        ElMessage.error('发布失败: ' + err)
        return
      }
      commentDialogVisible.value = false
      if (result?.message) ElMessage.warning(result.message)
      else ElMessage.success(commentPinned.value ? '评论已发布并置顶' : '评论已发布')
    })
  } catch (e) {
    commentPosting.value = false
    ElMessage.error('触发发布失败: ' + (e?.message || e))
  }
}

// 总评论数 = 顶级评论 + 各评论的回复数（与后端同步后的接口口径一致）
const commentTotal = computed(() =>
  comments.value.reduce((sum, c) => sum + 1 + (c.reply_count || 0), 0)
)

// 排序：置顶恒在最前；最热按点赞降序、最新按时间升序
const displayComments = computed(() => {
  const list = [...comments.value]
  const pin = c => Number(!!c.pinned)
  if (commentSort.value === 'hot') {
    list.sort((a, b) => pin(b) - pin(a) || b.like_count - a.like_count)
  } else {
    list.sort((a, b) => pin(b) - pin(a) || (a.ts || 0) - (b.ts || 0))
  }
  return list
})

// 楼中楼：收起时只显示第 1 条，展开显示全部
function visibleReplies(c) {
  const replies = c.replies || []
  return expandedReplies.has(c.id) ? replies : replies.slice(0, 1)
}

function repliesToggleLabel(c) {
  if (expandedReplies.has(c.id)) return '收起回复'
  if ((c.reply_count || 0) > 1) return `展开共 ${c.reply_count} 条回复`
  return ''
}

function toggleReplies(c) {
  if (expandedReplies.has(c.id)) expandedReplies.delete(c.id)
  else expandedReplies.add(c.id)
}

// 头像底色：按调色板循环（与项目 accent 一致）
const avatarColors = ['#8b5cf6', '#3b82f6', '#f43f5e', '#06b6d4', '#22c55e', '#f59e0b']
function avatarColor(i) {
  return avatarColors[i % avatarColors.length]
}

function toggleGroup(key) {
  if (expandedGroups.has(key)) expandedGroups.delete(key)
  else expandedGroups.add(key)
}

function selectAccount(account /*, group */) {
  selectedAccountId.value = account.id
}

async function fetchDetail() {
  error.value = ''
  loading.value = true
  try {
    const res = await historyApi.getBatch(route.params.batchId)
    // 拦截器只在 data.code === 200 时 resolve，否则 reject；到这里就是成功
    batch.value = res.data
    // 默认选中：找第一个 account_id 在 store 里能找到的 item
    const firstValid = batch.value.items.find(it =>
      it.account_id != null &&
      accountStore.accounts.some(a => a.id === it.account_id)
    )
    if (firstValid) selectedAccountId.value = firstValid.account_id
    // 展开所有有账号的组
    readonlyAccountGroups.value.forEach(g => expandedGroups.add(g.key))
  } catch (e) {
    // 拦截器已经 toast（4xx 用后端 msg，5xx 用通用文案）；这里只补行为
    if (e?.response?.status === 404) {
      // 批次不存在 → 跳回列表
      router.replace('/publish-history')
    } else if (e?.response?.status >= 500 || !e?.response) {
      // 服务端错误或网络错误 → 主区域顶部红条 + 重试按钮
      error.value = '加载失败，请稍后重试'
    } else {
      // 其它 4xx（401/403 等）→ 红条
      error.value = e.message || '加载失败'
    }
  } finally {
    loading.value = false
  }
}

// 静默刷新（不动 loading，避免轮询时整页转圈）
async function fetchDetailSilent() {
  try {
    const res = await historyApi.getBatch(route.params.batchId)
    batch.value = res.data
  } catch { /* 静默轮询失败忽略 */ }
}

// 批次还在等待间隔/发布中时每 5s 静默轮询：
// 倒计时归零后调度器开始发布，详情页状态自动翻转，无需手刷
const batchActive = computed(() =>
  batch.value && ['pending', 'queued', 'running'].includes(batch.value.status)
)
let pollTimer = null
watch(batchActive, (active) => {
  if (active && !pollTimer) {
    pollTimer = setInterval(fetchDetailSilent, 5000)
  } else if (!active && pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}, { immediate: true })
onBeforeUnmount(() => {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
  if (syncTimer) { clearInterval(syncTimer); syncTimer = null }
  if (commentsTimer) { clearInterval(commentsTimer); commentsTimer = null }
})

onMounted(async () => {
  // 串行：先加载账号 store，再拉详情
  try {
    if (accountStore.accounts.length === 0) {
      const res = await accountApi.getAccounts()
      accountStore.setAccounts(res.data || [])
    }
  } catch (e) {
    console.error('加载账号列表失败:', e)
  }
  await fetchDetail()
})
</script>

<style lang="scss" scoped>
@use '@/styles/variables.scss' as *;

.publish-history-detail {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: $bg-base;
  // 整体节奏：主区卡片间距 20px，卡片内 padding 20px，留白与卡片内文本密度平衡
}

.page-header {
  display: flex;
  align-items: center;
  gap: 16px;
  height: 56px;
  padding: 0 24px;
  border-bottom: 1px solid $border;
  background: $bg-elevated;
  flex-shrink: 0;

  .header-info {
    display: flex;
    align-items: center;
    gap: 12px;
    flex: 1;
    min-width: 0;
  }

  .batch-title {
    font-size: 16px;
    font-weight: 600;
    color: $text-primary;
    margin: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 400px;
    // 字距略紧，提升数据密集页的标题密度
    letter-spacing: -0.01em;
  }

  .header-time {
    font-size: 12px;
    color: $text-muted;
  }
}

.detail-body {
  flex: 1;
  display: flex;
  overflow: hidden;
}

.detail-sidebar {
  width: 232px;
  flex-shrink: 0;
  overflow-y: auto;
  // 左侧栏与主区用 1px 透明 border 拉出节奏（与右侧圆角卡呼应）
  border-right: 1px solid $border-light;
}

.detail-main {
  flex: 1;
  min-width: 0;
  overflow: hidden;  // 整页不滚动：详情固定一屏，仅评论列表内部滚动
  padding: 20px 24px 28px;  // 顶部 20px 与 56px header 形成 8 倍数节奏
  display: flex;
  flex-direction: column;
  gap: 20px;  // 卡片间距：8 倍数节奏
}

.status-tag {
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 500;
  letter-spacing: 0.02em;

  &.status-success, &.status-partial {
    background: rgba($accent-green, 0.15);
    color: #67c23a;
  }
  &.status-failed {
    background: rgba($danger-color, 0.15);
    color: #f56c6c;
  }
  &.status-running {
    background: rgba($info-color, 0.15);
    color: #409eff;
  }
  &.status-pending, &.status-cancelled {
    background: rgba(0, 0, 0, 0.06);
    color: $text-muted;
  }
}

// 发布链间隔等待中：距自动发布的倒计时标签（与状态 tag 并排）
.countdown-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 10px;
  border-radius: 10px;
  font-size: 12px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  background: rgba($brand-start, 0.12);
  color: $brand-start;
}

// 5xx 错误降级红条：使用项目 danger 色 + 8px 圆角 + 浅红底 + 1px 红边
.error-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 16px;
  background: rgba($danger-color, 0.1);
  border: 1px solid rgba($danger-color, 0.3);
  border-radius: 8px;
  color: #f56c6c;
  font-size: 14px;
  // 红条与下方卡片同节奏：margin-bottom 改为父级 gap 接管
}

.empty-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: $text-muted;
  text-align: center;
  gap: 8px;
  padding: 48px 16px;

  .empty-icon {
    font-size: 48px;
    opacity: 0.5;
  }

  p {
    margin: 0;
    font-size: 14px;
  }

  .empty-hint {
    font-size: 12px;
    a { color: $brand-start; text-decoration: none; }
    a:hover { text-decoration: underline; }
  }
}

// ===== 主区双栏布局：内容 Hero 卡 + 数据洞察栏 =====
.detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 288px;
  gap: 16px;
  height: 100%;
  min-height: 0;  // 允许在 flex 父级里收缩，撑出内部滚动空间

  // 窄窗口降级为单栏，恢复整页滚动（内容长，内部滚动反而难用）
  @media (max-width: 1080px) {
    height: auto;
    grid-template-columns: minmax(0, 1fr);

    .hero-cover { width: 200px; }
    .comment-list { max-height: 360px; }
  }
}

@media (max-width: 1080px) {
  .detail-main { overflow-y: auto; }
}

// Hero 卡：封面锚点 + 标题/身份/文案 + 底部操作栏（固定可见，不参与滚动）
.hero-card {
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  min-width: 0;
  background: $bg-elevated;
  border: 1px solid $border;
  border-radius: $radius-card;
  transition: $transition-base;

  &--failed {
    border-color: rgba($danger-color, 0.3);
    background: rgba($danger-color, 0.03);
  }

  // 失败横幅：卡片内顶部条，与卡片内边距对齐
  .error-banner { margin: 16px 20px 0; }
}

.error-banner {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  background: rgba($danger-color, 0.1);
  border: 1px solid rgba($danger-color, 0.3);
  border-radius: $radius-base;
  color: #f56c6c;

  .el-icon { flex-shrink: 0; }

  .error-banner-body {
    display: flex;
    flex-direction: column;
    gap: 2px;

    strong { font-size: 13px; font-weight: 600; }
    span { font-size: 12px; color: $text-secondary; word-break: break-all; }
  }
}

.hero-body {
  display: flex;
  gap: 20px;
  padding: 20px;
  min-width: 0;
}

.hero-cover {
  flex-shrink: 0;
  width: 240px;
  aspect-ratio: 16/9;
  background: $bg-surface;
  border: 1px solid $border;
  border-radius: $radius-base;
  overflow: hidden;
  position: relative;

  img { width: 100%; height: 100%; object-fit: cover; }
  .cover-placeholder {
    position: absolute; inset: 0;
    display: flex; align-items: center; justify-content: center;
    color: $text-muted;
  }
}

.hero-info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.hero-title-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;

  .status-tag {
    flex-shrink: 0;
    margin-top: 3px;
  }
}

.hero-title {
  font-size: 18px;
  font-weight: 600;
  color: $text-primary;
  margin: 0;
  line-height: 1.35;
  letter-spacing: -0.01em;
  min-width: 0;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

// 身份行：小头像 + 账号名 + 平台徽章
.identity-row {
  display: flex;
  align-items: center;
  gap: 8px;

  .avatar {
    width: 28px;
    height: 28px;
    border-radius: 50%;
    background: rgba($brand-start, 0.15);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 13px;
    color: #c4b5fd;
    font-weight: 700;
    border: 2px solid transparent;
    flex-shrink: 0;
  }

  .account-name {
    font-size: 14px;
    font-weight: 600;
    color: $text-primary;
  }

  .platform-badge {
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 10px;
    font-weight: 500;
    letter-spacing: 0.02em;
  }
}

.hero-desc {
  font-size: 13px;
  line-height: 1.6;
  color: $text-secondary;
  margin: 0;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.hero-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.hero-meta {
  display: flex;
  gap: 8px;
  font-size: 12px;
  color: $text-secondary;
  .meta-label { color: $text-muted; flex-shrink: 0; }
}

// 底部操作栏：元信息居左、动作居右，细分隔线收尾
.hero-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 12px 20px;
  border-top: 1px solid $border-light;

  .footer-meta {
    font-size: 12px;
    color: $text-muted;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
  }

  .hero-actions {
    display: flex;
    align-items: center;
    flex-shrink: 0;
  }

  // 已关联作品但平台不提供链接时的弱提示(如视频号)
  .bound-no-link {
    margin-left: 4px;
    font-size: 12px;
    color: $text-muted;
  }

  .view-link {
    margin-left: 12px;
    color: $brand-start;
    font-size: 13px;
    text-decoration: none;
    transition: opacity $transition-fast;
    &:hover { text-decoration: underline; opacity: 0.85; }
  }
}

// 洞察栏：整页不滚动，无需 sticky
.insight-rail {
  min-width: 0;
  min-height: 0;
}

// 左列：Hero 卡 + 评论卡纵向堆叠（评论卡吃掉剩余高度，内部滚动）
.detail-main-col {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
  min-height: 0;
}

// ===== 评论区 =====
.comments-card {
  flex: 1;
  min-height: 0;   // 关键：允许收缩，把滚动空间留给列表
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: $bg-elevated;
  border: 1px solid $border;
  border-radius: $radius-card;
  padding: 16px 20px;

  .comments-header {
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding-bottom: 12px;
    border-bottom: 1px solid $border-light;

    .section-title {
      font-size: 14px;
      font-weight: 600;
      color: $text-primary;
      margin: 0;
      display: flex;
      align-items: center;
      gap: 8px;
    }
  }
}

// 评论总数徽标
.comment-count {
  font-size: 12px;
  font-weight: 500;
  color: $text-muted;
  background: rgba($overlay-rgb, 0.06);
  padding: 1px 8px;
  border-radius: 10px;
}

// 评论列表空态 / 加载中
.comments-empty {
  padding: 40px 0;
  text-align: center;
  font-size: 13px;
  color: $text-muted;
}

.comments-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
}

// 最热 / 最新 切换胶囊
.sort-tabs {
  display: inline-flex;
  background: rgba($overlay-rgb, 0.05);
  border-radius: 8px;
  padding: 2px;

  button {
    border: none;
    background: transparent;
    font-family: inherit;
    font-size: 12px;
    color: $text-muted;
    padding: 4px 12px;
    border-radius: 6px;
    cursor: pointer;
    transition: $transition-fast;

    &.active {
      background: rgba($overlay-rgb, 0.1);
      color: $text-primary;
      font-weight: 500;
    }
    &:hover:not(.active) { color: $text-secondary; }
  }
}

.comment-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;   // 唯一滚动容器：评论列表内部滚动
  padding: 4px 8px 4px 0;  // 右侧留出滚动条间隙
}

.comment-item {
  display: flex;
  gap: 12px;
  padding: 14px 0;

  & + & { border-top: 1px solid $border-light; }
}

.comment-avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  font-weight: 600;
  color: #fff;
  flex-shrink: 0;
  user-select: none;
}

.comment-main {
  flex: 1;
  min-width: 0;
}

.comment-head {
  display: flex;
  align-items: center;
  gap: 8px;

  .comment-author {
    font-size: 13px;
    font-weight: 600;
    color: $text-secondary;
  }

  // UP主 / 置顶 徽标
  .author-chip {
    font-size: 10px;
    padding: 1px 6px;
    border-radius: 4px;
    background: rgba($brand-start, 0.15);
    color: $brand-start;
    font-weight: 500;
    flex-shrink: 0;
  }

  .pin-chip {
    font-size: 10px;
    padding: 1px 6px;
    border-radius: 4px;
    background: rgba($accent-amber, 0.15);
    color: $accent-amber;
    font-weight: 500;
    flex-shrink: 0;
  }

  .comment-time {
    margin-left: auto;
    font-size: 11px;
    color: $text-muted;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }
}

.comment-content {
  margin: 6px 0 8px;
  font-size: 13px;
  line-height: 1.65;
  color: $text-primary;
  word-break: break-word;
}

.comment-ops {
  display: flex;
  align-items: center;
  gap: 16px;

  .op {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 12px;
    color: $text-muted;
    cursor: pointer;
    transition: color $transition-fast;

    .el-icon { font-size: 14px; }
    &:hover { color: $brand-start; }
  }
}

// 楼中楼回复容器
.comment-replies {
  margin-top: 10px;
  padding: 4px 12px;
  background: rgba($overlay-rgb, 0.04);
  border-radius: $radius-sm;
}

.reply-item {
  display: flex;
  gap: 10px;
  padding: 8px 0;

  & + & { border-top: 1px solid rgba($overlay-rgb, 0.05); }
}

.reply-avatar {
  width: 26px;
  height: 26px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 600;
  color: #fff;
  flex-shrink: 0;
  user-select: none;
}

.reply-body {
  flex: 1;
  min-width: 0;

  .reply-head {
    display: flex;
    align-items: center;
    gap: 6px;

    .reply-author {
      font-size: 12px;
      font-weight: 600;
      color: $text-secondary;
    }

    .author-chip {
      font-size: 10px;
      padding: 0 6px;
      border-radius: 4px;
      background: rgba($brand-start, 0.15);
      color: $brand-start;
      font-weight: 500;
    }
  }

  .reply-content {
    margin: 4px 0;
    font-size: 12px;
    line-height: 1.6;
    color: $text-primary;
    word-break: break-word;
  }

  .reply-meta {
    font-size: 11px;
    color: $text-muted;
    font-variant-numeric: tabular-nums;
  }
}

// 展开 / 收起回复
.replies-toggle {
  margin: 2px 0 8px;
  border: none;
  background: transparent;
  padding: 0;
  font-family: inherit;
  font-size: 12px;
  color: $brand-start;
  cursor: pointer;

  &:hover { text-decoration: underline; }
}

// 数据统计：标题与内容 12px 分隔
.data-stats {
  background: $bg-elevated;
  border: 1px solid $border;
  border-radius: $radius-card;
  padding: 16px;

  .section-title {
    font-size: 14px;
    font-weight: 600;
    color: $text-primary;
    margin: 0 0 12px;
    letter-spacing: -0.005em;
    display: flex;
    align-items: baseline;
    gap: 8px;

    // 最近一次统计同步时间（MM-DD HH:mm）
    .synced-at {
      font-size: 12px;
      font-weight: 400;
      color: $text-muted;
    }
  }
}
</style>

<style lang="scss">
/* 一键评论弹窗（append-to-body 传送门内容，scoped 作用域覆盖不到，需全局） */
.comment-dialog-body {
  .comment-pin-check {
    margin-top: 12px;
  }
  .comment-unsupported {
    margin-top: 12px;
    font-size: 12px;
    color: #f59e0b;
  }
}
</style>
