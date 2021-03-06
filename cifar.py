#!/usr/bin/env python3
"""CIFAR-10 and CIFAR-100 helper script
"""

from __future__ import print_function

import inspect
import logging
import os
import random
import shutil
import tensorwatch as tw
import torch
import torchvision.datasets as datasets
import torchvision.transforms as transforms
from argparse import ArgumentParser
from pprint import pformat
from progress.bar import Bar
from tempfile import NamedTemporaryFile
from time import time
from torch.utils.data import DataLoader

import models.cifar as models
from utils import AverageMeter, Scribe, calculate_accuracy

MODEL_ARCHS = {
    name: value
    for name, value in inspect.getmembers(models)
    if inspect.isfunction(value) or inspect.ismodule(value)
}
USE_CUDA = torch.cuda.is_available()


def main(**args):
    t_logfile = NamedTemporaryFile(mode="w+", suffix=".log")
    logging.basicConfig(
        level=args["verbosity"],
        format="%(asctime)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(t_logfile.name)],
    )
    logging.info("Execution options: %s", pformat(args))

    # Preliminary Setup
    if USE_CUDA:
        os.environ["CUDA_VISIBLE_DEVICES"] = args["gpu_id"]
        logging.info("• CUDA is enabled")
        for device_id in args["gpu_id"].split():
            device_id = int(device_id)
            logging.info("%s", torch.cuda.get_device_name(device_id))
    else:
        logging.info("• CPU only (no CUDA)")
    seed = args["manual_seed"]
    if seed is None:
        seed = random.randint(1, 10000)
        logging.info("• Random Seed: %d", seed)
    random.seed(seed)
    torch.manual_seed(seed)
    if USE_CUDA:
        torch.cuda.manual_seed_all(seed)
    if not os.path.isdir(args["checkpoint"]):
        os.makedirs(args["checkpoint"], exist_ok=True)

    # Data
    logging.info("• Preparing '%(dataset)s' dataset", args)
    num_classes, trainloader, testloader = initialize_dataloaders(
        args["dataset"],
        workers=args["workers"],
        train_batch=args["train_batch"],
        test_batch=args["test_batch"],
    )

    # Model & Architecture
    arch = args["arch"]
    logging.info("• Initializing '%s' architecture", arch)
    model = initialize_model(arch, num_classes, **args)
    logging.info("%s", model)

    model = torch.nn.DataParallel(model)
    if USE_CUDA:
        model = model.cuda()
        torch.backends.cudnn.benchmark = True

    num_params = sum([p.numel() for p in model.parameters()])
    num_learnable = sum([p.numel() for p in model.parameters() if p.requires_grad])

    logging.info(
        "• Number of parameters: %(params)d (%(learnable)d learnable)",
        {"params": num_params, "learnable": num_learnable},
    )

    criterion = torch.nn.CrossEntropyLoss()
    optimizer = torch.optim.SGD(
        model.parameters(),
        lr=args["lr"],
        momentum=args["momentum"],
        weight_decay=args["weight_decay"],
    )

    # Tensorwatch Initialization
    w = tw.Watcher(filename=args["tensorwatch_log"])
    loss_stream = w.create_stream(name="train_loss")
    acc_stream = w.create_stream(name="train_acc")
    test_loss_stream = w.create_stream(name="test_loss")
    test_acc_stream = w.create_stream(name="test_acc")
    lr_stream = w.create_stream(name="lr")

    if args["mode"] == "evaluate":
        logging.info("Only evaluation")
        with torch.no_grad():
            test_loss, test_acc = test(testloader, model, criterion)
        logging.info(
            "Test Loss:  %(loss).8f, Test Acc:  %(acc).2f",
            {"loss": test_loss, "acc": test_acc},
        )

    elif args["mode"] == "train":
        best_acc = 0
        start_epoch = args["start_epoch"]
        title = args["dataset"] + "-" + arch

        scribe = Scribe(
            os.path.join(args["checkpoint"], "progress.txt"), title=title
        )
        scribe.set_names(
            [
                "Learning Rate",
                "Train Loss",
                "Valid Loss",
                "Train Acc.",
                "Valid Acc.",
            ]
        )

        lr = args["lr"]
        interrupted = False
        for epoch in range(start_epoch, args["epochs"]):
            train_loss, train_acc, test_loss, test_acc = 0, -1, 0, -1
            try:
                lr = update_learning_rate(
                    lr, args["schedule"], args["gamma"], optimizer, epoch
                )
                logging.info(
                    "Epoch %(cur_epoch)d/%(epochs)d | LR: %(lr)f",
                    {"cur_epoch": epoch + 1, "epochs": args["epochs"], "lr": lr},
                )
                train_loss, train_acc = train(trainloader, model, criterion, optimizer)
                with torch.no_grad():
                    test_loss, test_acc = test(testloader, model, criterion)
            except KeyboardInterrupt:
                logging.warning("Caught Keyboard Interrupt at epoch %d", epoch + 1)
                interrupted = True
            finally:
                # append model progress
                scribe.append((lr, train_loss, test_loss, train_acc, test_acc))
                loss_stream.write((epoch, train_loss))
                acc_stream.write((epoch, train_acc))
                test_loss_stream.write((epoch, test_loss))
                test_acc_stream.write((epoch, test_acc))
                lr_stream.write((epoch, lr))

                # save the model
                is_best = test_acc > best_acc
                best_acc = max(test_acc, best_acc)
                save_checkpoint(
                    {
                        "epoch": epoch + 1,
                        "state_dict": model.state_dict(),
                        "acc": test_acc,
                        "best_acc": best_acc,
                        "optimizer": optimizer.state_dict(),
                    },
                    is_best,
                    checkpoint=args["checkpoint"],
                )
            if interrupted:
                break

        scribe.close()
        scribe.plot(
            plot_title="Training Accuracy Progress",
            names=["Train Acc.", "Valid Acc."],
            xlabel="Epoch",
            ylabel="Accuracy",
        )
        scribe.savefig(os.path.join(args["checkpoint"], "progress_acc.eps"))
        scribe.plot(
            plot_title="Training Loss Progress",
            names=["Train Loss", "Valid Loss"],
            xlabel="Epoch",
            ylabel="Cross Entropy Loss",
        )
        scribe.savefig(os.path.join(args["checkpoint"], "progress_loss.eps"))
        logging.info("Best evaluation accuracy: %f", best_acc)
        logging.info("Results saved to %s", args["checkpoint"])

        shutil.copy(t_logfile.name, args["checkpoint"])
        t_logfile.close()

    elif args["mode"] == "profile":
        logging.info("Only profiling one pass, one input")
        for (inputs, _) in testloader:
            break
        logging.info("Input Size: %s", inputs.size())
        with torch.no_grad():
            if USE_CUDA:
                with torch.cuda.profiler.profile() as prof:
                    # warmup the CUDA memory allocator and profiler
                    # model(inputs)
                    with torch.autograd.profiler.emit_nvtx(enabled=USE_CUDA):
                        model(inputs)
            else:
                with torch.autograd.profiler.profile(use_cuda=USE_CUDA) as prof:
                    model(inputs)
            logging.info(prof)


def initialize_dataloaders(dataset, workers=0, train_batch=1, test_batch=1):
    base_transforms = [
        transforms.ToTensor(),
        # https://github.com/kuangliu/pytorch-cifar/issues/19#issue-268972488
        transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
    ]
    train_transforms = transforms.Compose(
        [transforms.RandomCrop(32, padding=4), transforms.RandomHorizontalFlip()]
        + base_transforms
    )
    test_transforms = transforms.Compose(base_transforms)
    if dataset == "cifar10":
        data_class = datasets.CIFAR10
        num_classes = 10
    elif dataset == "cifar100":
        data_class = datasets.CIFAR100
        num_classes = 100
    else:
        assert False, f"Unsupported dataset: {dataset}"

    trainset = data_class(
        root="./data", train=True, download=True, transform=train_transforms
    )
    trainloader = DataLoader(
        trainset, batch_size=train_batch, shuffle=True, num_workers=workers
    )
    testset = data_class(
        root="./data", train=False, download=False, transform=test_transforms
    )
    testloader = DataLoader(
        testset, batch_size=test_batch, shuffle=False, num_workers=workers
    )
    return num_classes, trainloader, testloader


def initialize_model(
    architecture,
    num_classes,
    cardinality=None,
    depth=None,
    widen_factor=None,
    drop=None,
    growth_rate=None,
    compression_rate=None,
    block_name=None,
    **kwargs,
):
    if architecture == "resnext":
        model = MODEL_ARCHS[architecture](
            cardinality=cardinality,
            num_classes=num_classes,
            depth=depth,
            widen_factor=widen_factor,
            dropRate=drop,
        )
    elif architecture == "densenet":
        model = MODEL_ARCHS[architecture](
            num_classes=num_classes,
            depth=depth,
            growthRate=growth_rate,
            compressionRate=compression_rate,
            dropRate=drop,
        )
    elif architecture == "wrn":
        model = MODEL_ARCHS[architecture](
            num_classes=num_classes,
            depth=depth,
            widen_factor=widen_factor,
            dropRate=drop,
        )
    elif architecture.endswith("resnet"):  # resnet & preresnet
        model = MODEL_ARCHS[architecture](
            num_classes=num_classes, depth=depth, block_name=block_name
        )
    else:  # alexnet, vgg*
        model = MODEL_ARCHS[architecture](num_classes=num_classes)
    return model


def train(trainloader, model, criterion, optimizer):
    return run_epoch_pass("Train", trainloader, model, criterion, optimizer)


def test(testloader, model, criterion):
    return run_epoch_pass("Test", testloader, model, criterion, None)


def run_epoch_pass(mode, dataloader, model, criterion, optimizer):
    """Perform one train or test pass through the data (epoch)
    """
    batch_time = AverageMeter("Batch Time")
    data_time = AverageMeter("Data Time")
    losses = AverageMeter("Losses")
    top1 = AverageMeter("Top 1 Accuracy")
    top5 = AverageMeter("Top 5 Accuracy")
    end = time()

    if mode == "Train":
        model.train()
    elif mode == "Test":
        model.eval()
    else:
        assert mode in ("Train", "Test"), f"Unsupported mode {mode}"

    bar = Bar(mode, max=len(dataloader))
    for batch_idx, (inputs, targets) in enumerate(dataloader):
        # measure data loading time
        data_time.update(time() - end)
        if USE_CUDA:
            inputs, targets = inputs.cuda(), targets.cuda()

        # compute output
        outputs = model(inputs)
        loss = criterion(outputs, targets)

        # measure accuracy and record loss
        # pylint: disable=unbalanced-tuple-unpacking
        prec1, prec5 = calculate_accuracy(outputs.data, targets.data, topk=(1, 5))
        losses.update(loss.data.item(), inputs.size(0))
        top1.update(prec1.item(), inputs.size(0))
        top5.update(prec5.item(), inputs.size(0))

        if mode == "Train":
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        # measure elapsed time
        batch_time.update(time() - end)
        end = time()

        # plot progress
        bar.suffix = "({batch}/{size}) Data: {data:.3f}s | Batch: {bt:.3f}s | Total: {total:} | ETA: {eta:} | Loss: {loss:.4f} | top1: {top1: .4f} | top5: {top5: .4f}".format(
            batch=batch_idx + 1,
            size=len(dataloader),
            data=data_time.avg,
            bt=batch_time.avg,
            total=bar.elapsed_td,
            eta=bar.eta_td,
            loss=losses.avg,
            top1=top1.avg,
            top5=top5.avg,
        )
        bar.next()
    bar.finish()
    return (losses.avg, top1.avg)


def update_learning_rate(lr, schedule, gamma, optimizer, epoch):
    if epoch in schedule:
        lr *= gamma
        for param_group in optimizer.param_groups:
            param_group["lr"] = lr
    return lr


def save_checkpoint(
    state, is_best, checkpoint="checkpoint", filename="checkpoint.pth.tar"
):
    filepath = os.path.join(checkpoint, filename)
    torch.save(state, filepath)
    if is_best:
        shutil.copyfile(filepath, os.path.join(checkpoint, "model_best.pth.tar"))


def parse_arguments():
    """Parse and return the command line argument dictionary object
    """
    parser = ArgumentParser("CIFAR-10/100 Training")
    _verbosity = "INFO"
    parser.add_argument(
        "-v",
        "--verbosity",
        type=str.upper,
        choices=logging._nameToLevel.keys(),
        default=_verbosity,
        metavar="VERBOSITY",
        help="output verbosity: {} (default: {})".format(
            " | ".join(logging._nameToLevel.keys()), _verbosity
        ),
    )
    parser.add_argument("--manual-seed", type=int, help="manual seed integer")
    _mode = "train"
    parser.add_argument(
        "-m",
        "--mode",
        type=str.lower,
        default=_mode,
        choices=["train", "evaluate", "profile"],
        help=f"script execution mode (default: {_mode})",
    )
    _tw_file = "tw.log"
    parser.add_argument(
        "-t",
        "--tensorwatch-log",
        type=str,
        default=_tw_file,
        help=f"tensorwatch log filename (default: {_tw_file})",
    )
    parser.add_argument(
        "--gpu-id", default="0", type=str, help="id(s) for CUDA_VISIBLE_DEVICES"
    )

    # Dataset Options
    d_op = parser.add_argument_group("Dataset")
    d_op.add_argument(
        "-d",
        "--dataset",
        default="cifar10",
        type=str.lower,
        choices=("cifar10", "cifar100"),
    )
    avail_cpus = min(4, len(os.sched_getaffinity(0)))
    d_op.add_argument(
        "-w",
        "--workers",
        default=avail_cpus,
        type=int,
        metavar="N",
        help=f"number of data-loader workers (default: {avail_cpus})",
    )

    # Architecture Options
    a_op = parser.add_argument_group("Architectures")
    _architecture = "alexnet"
    a_op.add_argument(
        "-a",
        "--arch",
        metavar="ARCH",
        default=_architecture,
        choices=MODEL_ARCHS.keys(),
        help="model architecture: {} (default: {})".format(
            " | ".join(MODEL_ARCHS.keys()), _architecture
        ),
    )
    _depth = 29
    a_op.add_argument(
        "--depth", type=int, default=_depth, help=f"Model depth (default: {_depth})"
    )
    _block_name = "basicblock"
    _block_choices = ["basicblock", "bottleneck"]
    a_op.add_argument(
        "--block-name",
        type=str.lower,
        default=_block_name,
        choices=_block_choices,
        help=f"Resnet|Preresnet building block: (default: {_block_name}",
    )
    _cardinality = 8
    a_op.add_argument(
        "--cardinality",
        type=int,
        default=_cardinality,
        help=f"Resnext cardinality (group) (default: {_cardinality})",
    )
    _widen_factor = 4
    a_op.add_argument(
        "--widen-factor",
        type=int,
        default=_widen_factor,
        help=f"Resnext|WRT widen factor, 4 -> 64, 8 -> 128, ... (default: {_widen_factor})",
    )
    _growth_rate = 12
    a_op.add_argument(
        "--growth-rate",
        type=int,
        default=_growth_rate,
        help=f"DenseNet growth rate (default: {_growth_rate}",
    )
    _compression_rate = 2
    a_op.add_argument(
        "--compressionRate",
        type=int,
        default=_compression_rate,
        help=f"DenseNet compression rate (theta) (default: {_compression_rate}",
    )

    # Optimization Options
    o_op = parser.add_argument_group("Optimizations")
    _epochs = 300
    o_op.add_argument(
        "--epochs",
        default=_epochs,
        type=int,
        metavar="N",
        help=f"number of epochs to run (default: {_epochs})",
    )
    _epoch_start = 0
    o_op.add_argument(
        "--start-epoch",
        default=_epoch_start,
        type=int,
        metavar="N",
        help=f"epoch start number (default: {_epoch_start})",
    )
    _train_batch = 128
    o_op.add_argument(
        "--train-batch",
        default=_train_batch,
        type=int,
        metavar="N",
        help=f"train batchsize (default: {_train_batch})",
    )
    _test_batch = 100
    o_op.add_argument(
        "--test-batch",
        default=_test_batch,
        type=int,
        metavar="N",
        help=f"test batchsize (default: {_test_batch})",
    )
    _lr = 0.1
    o_op.add_argument(
        "--lr",
        "--learning-rate",
        default=_lr,
        type=float,
        metavar="LR",
        help=f"initial learning rate (default: {_lr})",
    )
    _dropout = 0
    o_op.add_argument(
        "--drop",
        "--dropout",
        default=_dropout,
        type=float,
        metavar="Dropout",
        help=f"Dropout ratio (default: {_dropout})",
    )
    _schedule = [150, 225]
    o_op.add_argument(
        "--schedule",
        type=int,
        nargs="+",
        default=_schedule,
        help=f"Decrease LR at these epochs (default: {_schedule})",
    )
    _gamma = 0.1
    o_op.add_argument(
        "--gamma",
        type=float,
        default=_gamma,
        help=f"LR is multiplied by gamma on schedule (default: {_gamma})",
    )
    _momentum = 0.9
    o_op.add_argument(
        "--momentum",
        default=_momentum,
        type=float,
        metavar="M",
        help=f"momentum (default: {_momentum})",
    )
    _wd = 5e-4
    o_op.add_argument(
        "--weight-decay",
        "--wd",
        default=_wd,
        type=float,
        metavar="W",
        help=f"weight decay (default: {_wd})",
    )

    # Checkpoint Options
    c_op = parser.add_argument_group("Checkpoints")
    _checkpoint = "checkpoint"
    c_op.add_argument(
        "-c",
        "--checkpoint",
        default=_checkpoint,
        type=str,
        metavar="PATH",
        help=f"path to save checkpoint (default: {_checkpoint})",
    )

    return vars(parser.parse_args())


if __name__ == "__main__":
    options = parse_arguments()
    main(**options)
