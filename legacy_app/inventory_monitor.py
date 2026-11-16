"""
库存图片文件夹监控服务
监控指定文件夹，当有新图片添加时自动处理并更新库存
"""

import os
import time
import logging
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Optional

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class InventoryFolderMonitor:
    """库存文件夹监控器"""

    def __init__(self, db, watch_folder: str = None, warehouse_name: str = "默认仓库",
                 incremental: bool = True, check_interval: int = 10):
        """
        初始化监控器
        Args:
            db: Database实例
            watch_folder: 要监控的文件夹路径
            warehouse_name: 默认仓库名称
            incremental: 是否增量更新
            check_interval: 检查间隔（秒）
        """
        from inventory_ocr import InventoryUpdater

        self.db = db
        self.watch_folder = watch_folder or os.path.join(tempfile.gettempdir(), "inventory_upload")
        self.warehouse_name = warehouse_name
        self.incremental = incremental
        self.check_interval = check_interval
        self.updater = InventoryUpdater(db)
        self.running = False

        # 创建监控文件夹（如果不存在）
        os.makedirs(self.watch_folder, exist_ok=True)
        # 创建处理完成文件夹
        self.processed_folder = os.path.join(self.watch_folder, "processed")
        os.makedirs(self.processed_folder, exist_ok=True)
        # 创建失败文件夹
        self.failed_folder = os.path.join(self.watch_folder, "failed")
        os.makedirs(self.failed_folder, exist_ok=True)

        # 记录已处理的文件
        self.processed_files = set()
        self._load_processed_history()

    def _load_processed_history(self):
        """加载已处理文件的历史记录"""
        history_file = os.path.join(self.watch_folder, ".processed_history.txt")
        if os.path.exists(history_file):
            try:
                with open(history_file, 'r', encoding='utf-8') as f:
                    self.processed_files = set(line.strip() for line in f if line.strip())
                logger.info(f"加载了 {len(self.processed_files)} 条已处理文件记录")
            except Exception as e:
                logger.error(f"加载历史记录失败: {e}")

    def _save_processed_history(self):
        """保存已处理文件的历史记录"""
        history_file = os.path.join(self.watch_folder, ".processed_history.txt")
        try:
            with open(history_file, 'w', encoding='utf-8') as f:
                for filename in self.processed_files:
                    f.write(f"{filename}\n")
        except Exception as e:
            logger.error(f"保存历史记录失败: {e}")

    def _is_image_file(self, filename: str) -> bool:
        """判断是否是图片文件"""
        image_extensions = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tiff', '.webp'}
        return Path(filename).suffix.lower() in image_extensions

    def _move_file(self, src: str, dest_folder: str, status: str = "processed"):
        """移动文件到对应文件夹"""
        try:
            filename = os.path.basename(src)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            name, ext = os.path.splitext(filename)
            dest_name = f"{name}_{timestamp}_{status}{ext}"
            dest_path = os.path.join(dest_folder, dest_name)
            os.rename(src, dest_path)
            logger.info(f"文件已移动: {filename} -> {dest_name}")
        except Exception as e:
            logger.error(f"移动文件失败: {e}")

    def _process_single_image(self, image_path: str) -> bool:
        """
        处理单个图片文件
        Args:
            image_path: 图片文件路径

        Returns:
            是否处理成功
        """
        filename = os.path.basename(image_path)
        logger.info(f"开始处理图片: {filename}")

        try:
            # 尝试OCR识别
            try:
                from PIL import Image
                import pytesseract

                # 读取图片并OCR
                image = Image.open(image_path)
                text = pytesseract.image_to_string(image, lang='chi_sim+eng')

                if not text or len(text.strip()) < 10:
                    logger.warning(f"图片 {filename} 识别内容为空或太少")
                    self._move_file(image_path, self.failed_folder, "empty")
                    return False

                # 更新库存
                result = self.updater.update_from_text(
                    text,
                    self.warehouse_name,
                    self.incremental
                )

                logger.info(f"处理完成: 成功 {result.success_count}, 失败 {result.failed_count}")

                # 移动文件到已处理文件夹
                self._move_file(image_path, self.processed_folder, "ok")
                self.processed_files.add(filename)
                self._save_processed_history()

                return True

            except ImportError:
                logger.error("OCR库未安装，请运行: pip install pytesseract pillow")
                self._move_file(image_path, self.failed_folder, "no_ocr")
                return False

        except Exception as e:
            logger.error(f"处理图片 {filename} 失败: {e}")
            self._move_file(image_path, self.failed_folder, "error")
            return False

    def scan_once(self):
        """执行一次扫描"""
        logger.info("开始扫描文件夹...")

        # 获取所有图片文件
        image_files = []
        try:
            for filename in os.listdir(self.watch_folder):
                file_path = os.path.join(self.watch_folder, filename)

                # 跳过文件夹和隐藏文件
                if os.path.isdir(file_path) or filename.startswith('.'):
                    continue

                # 跳过已处理的文件
                if filename in self.processed_files:
                    continue

                # 只处理图片文件
                if self._is_image_file(filename):
                    image_files.append(file_path)
        except Exception as e:
            logger.error(f"扫描文件夹失败: {e}")
            return

        if not image_files:
            logger.info("没有新的图片文件")
            return

        logger.info(f"发现 {len(image_files)} 个新图片文件")

        # 处理每个图片
        success_count = 0
        for image_path in image_files:
            if self._process_single_image(image_path):
                success_count += 1

        logger.info(f"扫描完成: 处理了 {len(image_files)} 个文件, 成功 {success_count} 个")

    def start(self):
        """启动监控"""
        if self.running:
            logger.warning("监控已在运行中")
            return

        self.running = True
        logger.info(f"=" * 50)
        logger.info(f"库存图片监控服务启动")
        logger.info(f"监控文件夹: {self.watch_folder}")
        logger.info(f"仓库: {self.warehouse_name}")
        logger.info(f"更新模式: {'增量' if self.incremental else '覆盖'}")
        logger.info(f"检查间隔: {self.check_interval} 秒")
        logger.info(f"=" * 50)

        try:
            while self.running:
                self.scan_once()
                # 等待指定间隔
                for _ in range(self.check_interval * 10):
                    if not self.running:
                        break
                    time.sleep(0.1)
        except KeyboardInterrupt:
            logger.info("接收到停止信号")
        finally:
            self.running = False
            logger.info("监控服务已停止")

    def stop(self):
        """停止监控"""
        self.running = False
        logger.info("正在停止监控服务...")


def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="库存图片文件夹监控服务")
    parser.add_argument("--folder", "-f", help="要监控的文件夹路径")
    parser.add_argument("--warehouse", "-w", default="默认仓库", help="目标仓库名称")
    parser.add_argument("--interval", "-i", type=int, default=10, help="检查间隔（秒）")
    parser.add_argument("--overwrite", action="store_true", help="覆盖模式（默认为增量模式）")

    args = parser.parse_args()

    # 导入数据库
    from models import Database

    db = Database()

    # 创建监控器
    monitor = InventoryFolderMonitor(
        db=db,
        watch_folder=args.folder,
        warehouse_name=args.warehouse,
        incremental=not args.overwrite,
        check_interval=args.interval
    )

    # 启动监控
    monitor.start()


if __name__ == "__main__":
    main()
