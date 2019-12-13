import sys
import time
import argparse
import torch
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.nn import MSELoss, L1Loss
from dgl.model_zoo.chem.mpnn import MPNNModel
from gnn.data.dataset import train_validation_test_split
from gnn.data.qm9 import QM9Dataset
from gnn.data.dataloader import DataLoaderQM9
from gnn.metric import EarlyStopping
from gnn.utils import pickle_dump, seed_torch


def parse_args():
    parser = argparse.ArgumentParser(description="MPNN")

    # model
    parser.add_argument("--node-hidden-dim", type=int, default=64, help="")
    parser.add_argument("--edge-hidden-dim", type=int, default=128, help="")
    parser.add_argument("--num-step-message-passing", type=int, default=6, help="")
    parser.add_argument("--num-step-set2set", type=int, default=6, help="")
    parser.add_argument("--num-layer-set2set", type=int, default=3, help="")

    # training
    parser.add_argument(
        "--gpu", type=int, default=-1, help="which GPU to use. Set -1 to use CPU."
    )
    parser.add_argument(
        "--epochs", type=int, default=100, help="number of training epochs"
    )
    parser.add_argument("--lr", type=float, default=0.001, help="learning rate")
    parser.add_argument("--weight-decay", type=float, default=5e-4, help="weight decay")

    # output file (needed by hypertunity)
    parser.add_argument(
        "--output_file", type=str, default="results.pkl", help="name of output file"
    )

    args = parser.parse_args()

    if args.gpu >= 0 and torch.cuda.is_available():
        args.device = torch.device("cuda")
    else:
        args.device = None

    return args


def evaluate(model, data_loader, metric_fn, device=None):
    model.eval()

    with torch.no_grad():
        accuracy = 0.0
        count = 0

        for it, (bg, label) in enumerate(data_loader):
            nf = bg.ndata["feat"]
            ef = bg.edata["feat"]
            if device is not None:
                nf = nf.to(device=args.device)
                ef = ef.to(device=args.device)
                label = label.to(device=args.device)
            pred = model(bg, nf, ef)
            c = len(label)
            accuracy += metric_fn(pred, label) * c
            count += c

    return accuracy / count


def main(args):

    # dataset
    sdf_file = "/Users/mjwen/Documents/Dataset/qm9/gdb9_n200.sdf"
    label_file = "/Users/mjwen/Documents/Dataset/qm9/gdb9_n200.sdf.csv"
    props = ["u0_atom"]
    dataset = QM9Dataset(
        sdf_file, label_file, hetero=False, properties=props, unit_conversion=True
    )
    trainset, valset, testset = train_validation_test_split(
        dataset, validation=0.1, test=0.1
    )
    train_loader = DataLoaderQM9(trainset, hetero=False, batch_size=10, shuffle=True)
    val_loader = DataLoaderQM9(
        valset, hetero=False, batch_size=len(valset), shuffle=False
    )
    test_loader = DataLoaderQM9(
        testset, hetero=False, batch_size=len(testset), shuffle=False
    )

    # model
    in_feats = trainset.get_feature_size(["atom", "bond"])
    model = MPNNModel(
        node_input_dim=in_feats[0],
        edge_input_dim=in_feats[1],
        output_dim=len(props),
        node_hidden_dim=args.node_hidden_dim,
        edge_hidden_dim=args.edge_hidden_dim,
        num_step_message_passing=args.num_step_message_passing,
        num_step_set2set=args.num_step_set2set,
        num_layer_set2set=args.num_layer_set2set,
    )
    print(model)
    if args.device is not None:
        model.to(device=args.device)

    # optimizer and loss
    optimizer = torch.optim.Adam(
        model.parameters(), lr=args.lr, weight_decay=args.weight_decay
    )
    loss_func = MSELoss()

    # accuracy metric, learning rate scheduler, and stopper
    metric = L1Loss()
    patience = 150
    scheduler = ReduceLROnPlateau(
        optimizer, mode="min", factor=0.3, patience=patience // 3, verbose=True
    )
    stopper = EarlyStopping(patience=patience)

    print("\n\n# Epoch     Loss         TrainAcc        ValAcc     Time (s)")
    t0 = time.time()

    for epoch in range(args.epochs):
        ti = time.time()

        model.train()
        epoch_loss = 0
        epoch_pred = []
        epoch_label = []
        for it, (bg, label) in enumerate(train_loader):
            nf = bg.ndata["feat"]
            ef = bg.edata["feat"]
            if args.device is not None:
                nf = nf.to(device=args.device)
                ef = ef.to(device=args.device)
                label = label.to(device=args.device)
            pred = model(bg, nf, ef)
            loss = loss_func(pred, label)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            epoch_loss += loss.detach().item()
            epoch_pred.append(pred.detach())
            epoch_label.append(label.detach())

        epoch_loss /= it + 1

        # evaluate the accuracy
        train_acc = metric(torch.cat(epoch_pred), torch.cat(epoch_label))
        val_acc = evaluate(model, val_loader, metric, args.device)
        scheduler.step(val_acc)
        if stopper.step(val_acc, model, msg="epoch " + str(epoch)):
            # save results for hyperparam tune
            pickle_dump(float(stopper.best_score), args.output_file)
            break
        tt = time.time() - ti

        print(
            "{:5d}   {:12.6e}   {:12.6e}   {:12.6e}   {:.2f}".format(
                epoch, epoch_loss, train_acc, val_acc, tt
            )
        )
        if epoch % 10 == 0:
            sys.stdout.flush()

    # save results for hyperparam tune
    pickle_dump(float(stopper.best_score), args.output_file)

    # load best to calculate test accuracy
    model.load_state_dict(torch.load("es_checkpoint.pkl"))
    test_acc = evaluate(model, test_loader, metric, args.device)
    tt = time.time() - t0
    print("\n#TestAcc: {:12.6e} | Total time (s): {:.2f}\n".format(test_acc, tt))


if __name__ == "__main__":
    seed_torch()
    args = parse_args()
    print(args)
    main(args)
